"""Async Home Assistant websocket client.

Phase 1 uses only read APIs + the state_changed subscription. The
friendly-name override method exists for Phase 3 and is never called in
Phase 1. entity_id is NEVER modified.
"""
from __future__ import annotations

import asyncio
import itertools
import json
from typing import Callable

import websockets

from .models import Area, HAEntity, is_segment_entity
from .scope import ha_domain, ha_kind


class HAError(Exception):
    pass


class HAClient:
    def __init__(self, ws_url: str, token: str):
        self.ws_url = ws_url
        self.token = token
        self.ha_version: str | None = None
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._id = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}
        self._subs: dict[int, Callable[[dict], None]] = {}
        self._reader_task: asyncio.Task | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    async def connect(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._ws = await websockets.connect(self.ws_url, max_size=16 * 1024 * 1024)
        hello = json.loads(await self._ws.recv())
        if hello.get("type") != "auth_required":
            raise HAError(f"unexpected greeting from HA: {hello!r}")
        await self._ws.send(json.dumps({"type": "auth", "access_token": self.token}))
        res = json.loads(await self._ws.recv())
        if res.get("type") != "auth_ok":
            raise HAError(f"HA auth failed: {res.get('message', res)!r}")
        self.ha_version = res.get("ha_version")
        self._reader_task = asyncio.create_task(self._reader())

    async def _reader(self) -> None:
        try:
            async for raw in self._ws:  # type: ignore[union-attr]
                msg = json.loads(raw)
                mtype = msg.get("type")
                if mtype == "result":
                    fut = self._pending.pop(msg["id"], None)
                    if fut and not fut.done():
                        if msg.get("success"):
                            fut.set_result(msg.get("result"))
                        else:
                            fut.set_exception(HAError(str(msg.get("error"))))
                elif mtype == "event":
                    cb = self._subs.get(msg["id"])
                    if cb is not None:
                        try:
                            cb(msg["event"])
                        except Exception:
                            pass
        except asyncio.CancelledError:
            raise
        except Exception as e:  # connection dropped
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(HAError(f"HA connection lost: {e}"))

    async def _command(self, payload: dict):
        if self._ws is None or self._loop is None:
            raise HAError("not connected")
        mid = next(self._id)
        fut: asyncio.Future = self._loop.create_future()
        self._pending[mid] = fut
        await self._ws.send(json.dumps({**payload, "id": mid}))
        return await fut

    # --- read APIs ---
    async def list_areas(self) -> list[dict]:
        return await self._command({"type": "config/area_registry/list"})

    async def list_entities(self) -> list[dict]:
        return await self._command({"type": "config/entity_registry/list"})

    async def list_devices(self) -> list[dict]:
        return await self._command({"type": "config/device_registry/list"})

    async def get_states(self) -> list[dict]:
        return await self._command({"type": "get_states"})

    async def call_service(self, domain: str, service: str, data: dict | None = None):
        return await self._command({
            "type": "call_service", "domain": domain, "service": service,
            "service_data": data or {},
        })

    async def subscribe_state_changed(self, callback: Callable[[dict], None]) -> int:
        if self._ws is None or self._loop is None:
            raise HAError("not connected")
        mid = next(self._id)
        self._subs[mid] = callback
        fut: asyncio.Future = self._loop.create_future()
        self._pending[mid] = fut
        await self._ws.send(
            json.dumps({"id": mid, "type": "subscribe_events", "event_type": "state_changed"})
        )
        await fut  # wait for subscribe ack
        return mid

    # --- write API (Phase 3 only; friendly-name override, never entity_id) ---
    async def set_entity_name(self, entity_id: str, name: str | None):
        return await self._command(
            {"type": "config/entity_registry/update", "entity_id": entity_id, "name": name}
        )

    async def close(self) -> None:
        if self._reader_task:
            self._reader_task.cancel()
        if self._ws:
            await self._ws.close()


def build_entities(
    areas: list[dict], entities: list[dict], devices: list[dict],
    state_names: dict[str, str] | None = None,
) -> tuple[list[Area], list[HAEntity]]:
    """Resolve in-scope entities (actuators + sensors) to their effective area
    (entity area, else device area). Out-of-scope domains are skipped.

    Display name precedence: registry name override > state friendly_name >
    registry original_name > entity_id."""
    state_names = state_names or {}
    area_objs = [Area(a["area_id"], a["name"]) for a in areas]
    area_name = {a.area_id: a.name for a in area_objs}
    device_area = {d["id"]: d.get("area_id") for d in devices}

    out: list[HAEntity] = []
    for e in entities:
        eid = e.get("entity_id", "")
        kind = ha_kind(eid)
        if kind is None:
            continue
        if e.get("disabled_by"):
            continue
        # Skip auxiliary config/diagnostic entities (e.g. Hue "Automation:"
        # toggles, power-on-behavior switches). Primary devices have no category.
        if e.get("entity_category"):
            continue
        aid = e.get("area_id") or device_area.get(e.get("device_id"))
        fname = (
            e.get("name") or state_names.get(eid) or e.get("original_name") or eid
        )
        out.append(
            HAEntity(eid, fname, ha_domain(eid), kind, aid, area_name.get(aid),
                     is_segment_entity(eid))
        )
    out.sort(key=lambda x: x.friendly_name.lower())
    area_objs.sort(key=lambda x: x.name.lower())
    return area_objs, out
