"""HomeKit access via the HomeClaw MCP SSE proxy.

The MCP session (sse_client + ClientSession) is owned by ONE dedicated task
for its whole lifetime. Callers submit requests over a queue and await a
future. This is required because anyio task groups used by the MCP SDK must
be entered and exited in the same task — opening in on_mount and closing
elsewhere raises "exit cancel scope in a different task". The owner-task
pattern keeps the session warm (one proxy subprocess) and avoids that.

read_timeout_seconds is generous because device_map is slow and large (its
reply exceeds 64 KiB; the proxy was patched to allow that).
"""
from __future__ import annotations

import asyncio
import json
from datetime import timedelta

from mcp import ClientSession
from mcp.client.sse import sse_client

from .models import HKAccessory

_SHUTDOWN = object()


class HomeKitError(Exception):
    pass


class HomeKitClient:
    def __init__(self, sse_url: str, home_id: str | None = None):
        self.sse_url = sse_url
        self.home_id = home_id or None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._req_q: asyncio.Queue = asyncio.Queue()
        self._owner: asyncio.Task | None = None
        self._ready = asyncio.Event()
        self._start_error: Exception | None = None

    async def connect(self) -> dict:
        self._loop = asyncio.get_running_loop()
        self._owner = asyncio.create_task(self._owner_loop())
        await self._ready.wait()
        if self._start_error:
            raise HomeKitError(f"HomeKit connect failed: {self._start_error}")
        status = await self.status()
        if not status.get("ready"):
            raise HomeKitError(f"HomeKit proxy not ready: {status!r}")
        return status

    async def _owner_loop(self) -> None:
        try:
            async with sse_client(self.sse_url) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    self._ready.set()
                    while True:
                        item = await self._req_q.get()
                        if item is _SHUTDOWN:
                            break
                        fut, name, args, tmo = item
                        if fut.cancelled():
                            continue
                        try:
                            res = await session.call_tool(
                                name, args,
                                read_timeout_seconds=timedelta(seconds=tmo),
                            )
                            self._loop.call_soon_threadsafe(_set, fut, res, None)
                        except Exception as e:  # noqa: BLE001
                            self._loop.call_soon_threadsafe(_set, fut, None, e)
        except Exception as e:  # noqa: BLE001 — connection/init failure
            self._start_error = e
            self._ready.set()

    async def _call(self, name: str, args: dict | None = None, tmo: int = 120):
        if self._owner is None or self._owner.done():
            raise HomeKitError("not connected")
        fut: asyncio.Future = self._loop.create_future()  # type: ignore[union-attr]
        await self._req_q.put((fut, name, args or {}, tmo))
        res = await fut
        text = res.content[0].text if res.content else "null"
        if getattr(res, "isError", False):
            raise HomeKitError(f"{name} returned error: {text}")
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            raise HomeKitError(f"{name} returned non-JSON: {text[:200]}") from e

    def _home_args(self) -> dict:
        return {"home_id": self.home_id} if self.home_id else {}

    async def status(self) -> dict:
        return await self._call("homekit_status", {}, 30)

    async def device_map(self) -> dict:
        return await self._call("homekit_device_map", self._home_args(), 180)

    async def rooms(self) -> list[dict]:
        return await self._call("homekit_rooms", self._home_args(), 60)

    async def events(self, since: str | None = None, etype: str | None = None,
                     limit: int = 200) -> list[dict]:
        args: dict = {"limit": limit}
        if since:
            args["since"] = since
        if etype:
            args["type"] = etype
        out = await self._call("homekit_events", args, 60)
        return out.get("events", []) if isinstance(out, dict) else []

    # --- writes (Phase 2/3; always call with dry_run first) ---
    async def assign_rooms(self, assignments: list[dict], dry_run: bool = True):
        return await self._call(
            "homekit_manage",
            {"action": "assign_rooms", "assignments": assignments,
             "dry_run": dry_run, **self._home_args()},
            120,
        )

    async def rename(self, uuid: str, new_name: str, dry_run: bool = True):
        return await self._call(
            "homekit_manage",
            {"action": "rename", "id": uuid, "new_name": new_name,
             "dry_run": dry_run, **self._home_args()},
            120,
        )

    async def close(self) -> None:
        if self._owner and not self._owner.done():
            await self._req_q.put(_SHUTDOWN)
            try:
                await asyncio.wait_for(self._owner, timeout=10)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._owner.cancel()
        self._owner = None


def _set(fut: asyncio.Future, result, exc) -> None:
    if fut.done():
        return
    if exc is not None:
        fut.set_exception(exc)
    else:
        fut.set_result(result)


def parse_accessories(device_map: dict) -> list[HKAccessory]:
    """Flatten device_map homes->zones->rooms->devices into accessories."""
    out: list[HKAccessory] = []
    for home in device_map.get("homes", []):
        for zone in home.get("zones", []):
            for room in zone.get("rooms", []):
                rname = room.get("name")
                for dev in room.get("devices", []):
                    out.append(
                        HKAccessory(
                            uuid=dev["id"],
                            name=dev.get("name", ""),
                            room=rname,
                            reachable=bool(dev.get("reachable", False)),
                            semantic_type=dev.get("semantic_type"),
                            category=dev.get("category"),
                        )
                    )
    return out
