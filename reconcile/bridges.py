"""HA→HomeKit bridge operations over the REST API.

In this setup there's one HomeKit bridge per room; a device's room follows
whichever bridge exposes it (a HomeClaw room-set reverts). The durable move is
to change a bridge's include_entities, via the integration's options flow,
followed by an explicit config-entry reload to re-publish (no full HA restart).
Validated 2026-05-31. A bridge move changes the HomeKit UUID, so callers must
re-key on the serial (= entity_id).
"""
from __future__ import annotations

import aiohttp


class BridgeError(Exception):
    pass


class BridgeManager:
    def __init__(self, http_base: str, token: str):
        self.base = http_base.rstrip("/")
        self.hdr = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    async def _homekit_entries(self, s: aiohttp.ClientSession) -> list[dict]:
        async with s.get(f"{self.base}/api/config/config_entries/entry", headers=self.hdr) as r:
            data = await r.json()
        return [e for e in data if e.get("domain") == "homekit"]

    async def _start(self, s, entry_id):
        async with s.post(f"{self.base}/api/config/config_entries/options/flow",
                          headers=self.hdr, json={"handler": entry_id}) as r:
            init = await r.json()
        fid = init.get("flow_id")
        if not fid:
            raise BridgeError(f"could not start options flow for {entry_id}: {init}")
        vals = {f["name"]: f.get("default") for f in init.get("data_schema", [])}
        return fid, vals

    async def _submit(self, s, fid, payload):
        async with s.post(f"{self.base}/api/config/config_entries/options/flow/{fid}",
                          headers=self.hdr, json=payload) as r:
            return await r.json()

    async def _delete(self, s, fid):
        try:
            async with s.delete(f"{self.base}/api/config/config_entries/options/flow/{fid}",
                                headers=self.hdr) as r:
                await r.read()
        except Exception:
            pass

    async def snapshot(self) -> list[dict]:
        """All HomeKit bridges as {entry_id, title, include_entities, mode}."""
        out = []
        async with aiohttp.ClientSession() as s:
            for e in await self._homekit_entries(s):
                eid = e["entry_id"]
                inc, mode = [], "include"
                try:
                    fid, vals = await self._start(s, eid)
                    mode = vals.get("include_exclude_mode") or "include"
                    step = await self._submit(s, fid, {
                        "mode": vals.get("mode", "bridge"),
                        "include_exclude_mode": mode,
                        "domains": vals.get("domains") or [],
                    })
                    inc = next((f.get("default") or [] for f in step.get("data_schema", [])
                                if f["name"] == "entities"), [])
                    await self._delete(s, fid)
                except Exception:
                    pass
                out.append({"entry_id": eid, "title": (e.get("title") or "").strip(),
                            "include_entities": list(inc), "mode": mode})
        return out

    async def set_include(self, entry_id: str, entities: list[str]) -> str:
        """Set a bridge's include_entities to exactly `entities` (include mode)."""
        async with aiohttp.ClientSession() as s:
            fid, vals = await self._start(s, entry_id)
            if (vals.get("include_exclude_mode") or "include") != "include":
                await self._delete(s, fid)
                raise BridgeError(f"bridge {entry_id} is not in include mode")
            cur_domains = set(vals.get("domains") or [])
            domains = sorted(cur_domains | {e.split(".", 1)[0] for e in entities})
            step = await self._submit(s, fid, {
                "mode": vals.get("mode", "bridge"),
                "include_exclude_mode": "include",
                "domains": domains,
            })
            if step.get("step_id") != "include":
                await self._delete(s, fid)
                raise BridgeError(f"unexpected step {step.get('step_id')!r} for {entry_id}")
            done = await self._submit(s, fid, {"entities": entities})
            if done.get("type") != "create_entry":
                raise BridgeError(f"set_include did not apply for {entry_id}: {done}")
            return done.get("type")

    async def reload(self, entry_ids: list[str]) -> None:
        """Reload config entries so the bridges re-publish (no HA restart)."""
        async with aiohttp.ClientSession() as s:
            for eid in entry_ids:
                async with s.post(f"{self.base}/api/config/config_entries/entry/{eid}/reload",
                                  headers=self.hdr) as r:
                    await r.read()

    async def create_bridge(self, title: str) -> str:
        """Create a new HomeKit bridge with the given title. Returns entry_id.
        The bridge will need pairing in the Home app before it publishes devices."""
        async with aiohttp.ClientSession() as s:
            async with s.post(f"{self.base}/api/config/config_entries/flow",
                              headers=self.hdr, json={"handler": "homekit"}) as r:
                init = await r.json()
            flow_id = init.get("flow_id")
            if not flow_id:
                raise BridgeError(f"could not start config flow: {init}")
            if init.get("type") == "abort":
                raise BridgeError(f"flow aborted at start: {init.get('reason')}")
            # Only submit keys the schema declares — never inject extras.
            schema = init.get("data_schema", [])
            schema_keys = {f["name"] for f in schema}
            payload = {f["name"]: f["default"] for f in schema if "default" in f}
            if "name" in schema_keys:
                payload["name"] = title
            async with s.post(f"{self.base}/api/config/config_entries/flow/{flow_id}",
                              headers=self.hdr, json=payload) as r:
                result = await r.json()
            # Walk remaining steps (some HA versions have a pairing/confirm step).
            for _ in range(5):
                rtype = result.get("type")
                if rtype == "create_entry":
                    entry_id = ((result.get("result") or {}).get("entry_id")
                                or result.get("entry_id"))
                    if not entry_id:
                        raise BridgeError(f"no entry_id in create_entry: {result}")
                    return entry_id
                if rtype == "abort":
                    raise BridgeError(f"flow aborted: {result.get('reason')}")
                if rtype != "form":
                    break
                # Submit next step with schema defaults only.
                step_schema = result.get("data_schema", [])
                step_keys = {f["name"] for f in step_schema}
                step_payload = {f["name"]: f["default"]
                                for f in step_schema if "default" in f}
                if "name" in step_keys:
                    step_payload["name"] = title
                async with s.post(
                        f"{self.base}/api/config/config_entries/flow/{flow_id}",
                        headers=self.hdr, json=step_payload) as r:
                    result = await r.json()
            try:
                async with s.delete(
                        f"{self.base}/api/config/config_entries/flow/{flow_id}",
                        headers=self.hdr) as r:
                    await r.read()
            except Exception:
                pass
            raise BridgeError(f"unexpected flow result: {result}")

    async def delete_bridge(self, entry_id: str) -> None:
        """Delete a HomeKit bridge config entry."""
        async with aiohttp.ClientSession() as s:
            async with s.delete(f"{self.base}/api/config/config_entries/entry/{entry_id}",
                                headers=self.hdr) as r:
                await r.read()
