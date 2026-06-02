"""ReconcileSession: connection + inventory + listen-mode state.

UI-agnostic so both the TUI and the headless self-test use the same logic.
Phase 1 is read-only: nothing here writes to HA or HomeKit.
"""
from __future__ import annotations

from datetime import datetime, timezone

from .config import Config
from .ha_client import HAClient, build_entities
from .homekit_client import HomeKitClient, parse_accessories
import collections

from .bridges import BridgeManager
from .matcher import Correlation, correlate
from .models import Area, HAEntity, HKAccessory, MoveItem, PlannedAssignment, PlannedRename
from .phase2 import Plan, build_plan, normalize_room
from .phase3 import RenamePlan, build_rename_plan, normalize_name
from .store import MappingStore


def _http_base(ws_url: str) -> str:
    return (ws_url.replace("wss://", "https://").replace("ws://", "http://")
            .replace("/api/websocket", "").rstrip("/"))


class ReconcileSession:
    def __init__(self, config: Config, store_path: str = "mappings.json"):
        self.cfg = config
        self.ha = HAClient(config.ha_ws_url, config.ha_token)
        self.hk = HomeKitClient(config.hk_sse_url, config.hk_home_id)
        self.bridges = BridgeManager(_http_base(config.ha_ws_url), config.ha_token)
        self.store = MappingStore(store_path)

        self.areas: list[Area] = []
        self.entities: list[HAEntity] = []
        self.hk_accessories: list[HKAccessory] = []
        self.hk_by_uuid: dict[str, HKAccessory] = {}

        self.ha_version: str | None = None
        self.hk_status: dict | None = None

        # Bridge membership = the durable HomeKit room. Cached for the table.
        self.bridge_list: list[dict] = []
        self.bridge_map: dict[str, dict] = {}   # entity_id -> {"title", "area"}
        self.bridges_loaded = False

        # display/scope toggles
        self.show_segments = False    # hide LED-strip segment sub-entities
        self.include_sensors = False  # door/motion/water sensors are opt-in

        # Phase 2/3: batch apply stays locked until one real write is verified.
        self.batch_unlocked = False         # Phase 2 (room assignment)
        self.rename_batch_unlocked = False  # Phase 3 (rename)

        self.listening = False
        self._buffer: list[dict] = []
        self._listen_since: str | None = None

    # --- connection / inventory ---
    def _on_state_changed(self, event: dict) -> None:
        if self.listening:
            data = event.get("data")
            if data:
                self._buffer.append(data)

    async def connect(self) -> None:
        await self.ha.connect()
        self.ha_version = self.ha.ha_version
        self.hk_status = await self.hk.connect()
        await self.ha.subscribe_state_changed(self._on_state_changed)

    async def load_inventory(self) -> None:
        areas_raw = await self.ha.list_areas()
        entities = await self.ha.list_entities()
        devices = await self.ha.list_devices()
        states = await self.ha.get_states()
        state_names = {
            s["entity_id"]: s.get("attributes", {}).get("friendly_name")
            for s in states
            if s.get("attributes", {}).get("friendly_name")
        }
        self.areas, self.entities = build_entities(
            areas_raw, entities, devices, state_names
        )

        device_map = await self.hk.device_map()
        self.hk_accessories = parse_accessories(device_map)
        self.hk_by_uuid = {h.uuid: h for h in self.hk_accessories}

    # --- queries ---
    def _visible(self, e: HAEntity) -> bool:
        if e.kind == "sensor" and not self.include_sensors:
            return False
        if e.is_segment and not self.show_segments:
            return False
        return True

    def entities_in_area(self, area_id: str | None) -> list[HAEntity]:
        return [e for e in self.entities if e.area_id == area_id and self._visible(e)]

    def shown_count(self) -> int:
        return sum(1 for e in self.entities if self._visible(e))

    def segment_count(self) -> int:
        return sum(1 for e in self.entities if e.is_segment)

    def sensor_count(self) -> int:
        return sum(1 for e in self.entities if e.kind == "sensor")

    def match_for(self, entity_id: str) -> dict | None:
        return self.store.get_by_entity(entity_id)

    # --- Move: durable room change via HA→HomeKit bridge membership ---
    def _area_of(self, entity_id: str) -> str | None:
        e = next((x for x in self.entities if x.entity_id == entity_id), None)
        return e.area_name if e else None

    def _friendly_of(self, entity_id: str) -> str:
        e = next((x for x in self.entities if x.entity_id == entity_id), None)
        return e.friendly_name if e else entity_id

    def _bridge_area(self, title: str, include: list[str], area_names: list[str]) -> str | None:
        """Resolve a bridge to an HA area: prefer a title↔area name match (handles
        the contaminated 'Violet' bridge), fall back to the dominant area of its
        entities (handles opaque titles like 'HASS Bridge XC' = Back Porch)."""
        tn = normalize_room(title)
        for a in area_names:
            an = normalize_room(a)
            if an and (an in tn or tn in an):
                return a
        areas = [self._area_of(e) for e in include]
        areas = [a for a in areas if a]
        return collections.Counter(areas).most_common(1)[0][0] if areas else None

    async def bridge_snapshot(self) -> list[dict]:
        raw = await self.bridges.snapshot()
        area_names = [a.name for a in self.areas]
        for b in raw:
            b["area"] = self._bridge_area(b["title"], b["include_entities"], area_names)
        return raw

    async def load_bridges(self) -> None:
        """Refresh the cached entity→bridge map (the durable HomeKit room)."""
        bl = await self.bridge_snapshot()
        m: dict[str, dict] = {}
        for b in bl:
            for eid in b["include_entities"]:
                m[eid] = {"title": b["title"], "area": b["area"]}
        self.bridge_list = bl
        self.bridge_map = m
        self.bridges_loaded = True

    def bridge_area_of(self, entity_id: str) -> str | None:
        info = self.bridge_map.get(entity_id)
        return info["area"] if info else None

    def plan_move(self, entity_ids: list[str], target_area: str,
                  bridges: list[dict]) -> tuple[list[MoveItem], dict | None]:
        target = next((b for b in bridges if b.get("area") == target_area), None)
        items: list[MoveItem] = []
        for eid in entity_ids:
            fr = self._friendly_of(eid)
            src = next((b for b in bridges if eid in b["include_entities"]), None)
            st = src["title"] if src else None
            se = src["entry_id"] if src else None
            if target is None:
                items.append(MoveItem(eid, fr, st, se, None, None, "no_target",
                                      f"no bridge maps to {target_area}",
                                      target_area=target_area))
            elif se == target["entry_id"]:
                items.append(MoveItem(eid, fr, st, se, target["title"], target["entry_id"],
                                      "already", "already on this bridge"))
            else:
                items.append(MoveItem(eid, fr, st, se, target["title"], target["entry_id"],
                                      "move", "" if src else "not currently on any bridge"))
        return items, target

    def plan_move_auto(self, entity_ids: list[str], bridges: list[dict]) -> list[MoveItem]:
        """Per-device plan: each device targets ITS OWN HA area's bridge (the fix)."""
        items: list[MoveItem] = []
        for eid in entity_ids:
            area = self._area_of(eid)
            if area is None:
                src = next((b for b in bridges if eid in b["include_entities"]), None)
                items.append(MoveItem(
                    eid, self._friendly_of(eid), src["title"] if src else None,
                    src["entry_id"] if src else None, None, None,
                    "no_target", "no HA area — pick a room"))
            else:
                sub, _ = self.plan_move([eid], area, bridges)
                items.extend(sub)
        return items

    async def create_bridge(self, area_name: str) -> str:
        """Create a new HomeKit bridge named after an area. Returns entry_id."""
        return await self.bridges.create_bridge(area_name)

    async def delete_bridge(self, entry_id: str) -> None:
        """Delete a HomeKit bridge config entry."""
        await self.bridges.delete_bridge(entry_id)

    async def apply_move(self, items: list[MoveItem], bridges: list[dict]) -> list[str]:
        """Remove moved entities from their source bridges, add each to ITS target
        (items may have different targets), then reload changed bridges."""
        by_entry = {b["entry_id"]: list(b["include_entities"]) for b in bridges}
        changed: set[str] = set()
        for it in items:
            if it.status != "move":
                continue
            if it.source_entry and it.entity_id in by_entry.get(it.source_entry, []):
                by_entry[it.source_entry].remove(it.entity_id)
                changed.add(it.source_entry)
            te = it.target_entry
            if te and it.entity_id not in by_entry.get(te, []):
                by_entry.setdefault(te, []).append(it.entity_id)
                changed.add(te)
        for entry in changed:
            await self.bridges.set_include(entry, by_entry[entry])
        await self.bridges.reload(list(changed))
        return list(changed)

    async def toggle_entity(self, entity_id: str) -> None:
        """Activate/toggle a device from HA (to identify/test it)."""
        domain = entity_id.split(".", 1)[0]
        if domain == "button":
            await self.ha.call_service("button", "press", {"entity_id": entity_id})
        elif domain == "cover":
            await self.ha.call_service("cover", "toggle", {"entity_id": entity_id})
        else:
            await self.ha.call_service("homeassistant", "toggle", {"entity_id": entity_id})

    # --- listen mode ---
    async def start_listen(self) -> None:
        """Begin a correlation window. Anchor 'since' to the HomeKit server's
        own clock (newest event timestamp) to sidestep host clock skew."""
        self._buffer = []
        try:
            recent = await self.hk.events(limit=1)
        except Exception:
            recent = []
        if recent and recent[0].get("timestamp"):
            self._listen_since = recent[0]["timestamp"]
        else:
            self._listen_since = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.listening = True

    @property
    def listen_since(self) -> str | None:
        return self._listen_since

    def ha_buffer(self) -> list[dict]:
        """Snapshot of state_changed data captured since listen start."""
        return list(self._buffer)

    async def correlate_now(self) -> Correlation:
        hk_events = await self.hk.events(
            since=self._listen_since, etype="characteristic_change", limit=300
        )
        return correlate(self._buffer, hk_events, self.include_sensors)

    def stop_listen(self) -> None:
        self.listening = False
        self._buffer = []

    def confirm_match(
        self, entity_id: str, uuid: str,
        fallback_name: str | None = None, fallback_room: str | None = None,
    ) -> dict:
        ent = next((e for e in self.entities if e.entity_id == entity_id), None)
        hk = self.hk_by_uuid.get(uuid)
        mapping = {
            "entity_id": entity_id,
            "uuid": uuid,
            "hk_name": (hk.name if hk else None) or fallback_name or "",
            "ha_friendly": ent.friendly_name if ent else entity_id,
            "room_at_match": (hk.room if hk else None) or fallback_room,
        }
        self.store.upsert(mapping)
        return mapping

    # --- Phase 2: room assignment (writes, gated) ---
    async def _refresh_hk(self) -> None:
        """Re-read device_map and refresh the cached accessory index."""
        device_map = await self.hk.device_map()
        self.hk_accessories = parse_accessories(device_map)
        self.hk_by_uuid = {h.uuid: h for h in self.hk_accessories}

    async def build_plan(self) -> Plan:
        """Re-read device_map (cache may be stale) and stage assignments."""
        await self._refresh_hk()
        existing_rooms = sorted({h.room for h in self.hk_accessories if h.room})
        ent_by_id = {e.entity_id: e for e in self.entities}
        return build_plan(self.store.all(), ent_by_id, self.hk_by_uuid, existing_rooms)

    async def assign_uuid_plan(self, uuid: str, target_area: str) -> PlannedAssignment:
        """Build a single room-assignment for an accessory by UUID (fresh
        device_map), reusing an existing room's exact spelling if it matches."""
        await self._refresh_hk()
        hk = self.hk_by_uuid.get(uuid)
        existing = {normalize_room(a.room): a.room for a in self.hk_accessories if a.room}
        target = existing.get(normalize_room(target_area), target_area)
        creates = normalize_room(target_area) not in existing
        return PlannedAssignment(
            "", uuid, hk.name if hk else "", hk.room if hk else None,
            target, target_area, True, creates)

    async def plan_for_entity(self, entity_id: str) -> PlannedAssignment | None:
        """Re-read device_map and return the room-assignment for one mapping,
        or None if it's already aligned / not actionable."""
        plan = await self.build_plan()
        return next((p for p in plan.assignments if p.entity_id == entity_id), None)

    @staticmethod
    def _payload(plans: list[PlannedAssignment]) -> list[dict]:
        return [{"uuid": p.uuid, "room": p.target_room} for p in plans]

    async def assign_dry_run(self, plans: list[PlannedAssignment]) -> dict:
        return await self.hk.assign_rooms(self._payload(plans), dry_run=True)

    async def assign_apply(self, plans: list[PlannedAssignment]) -> dict:
        return await self.hk.assign_rooms(self._payload(plans), dry_run=False)

    async def verify_assignment(
        self, plans: list[PlannedAssignment]
    ) -> list[tuple[PlannedAssignment, str | None, bool]]:
        """Re-read device_map and confirm each accessory now reports target room.
        Does not trust the assign_rooms return shape."""
        await self._refresh_hk()
        results = []
        for p in plans:
            hk = self.hk_by_uuid.get(p.uuid)
            actual = hk.room if hk else None
            ok = actual is not None and normalize_room(actual) == normalize_room(p.target_room)
            if ok:
                self._mark_assigned(p, actual)
            results.append((p, actual, ok))
        return results

    def _mark_assigned(self, p: PlannedAssignment, actual_room: str | None) -> None:
        mapping = self.store.get_by_entity(p.entity_id)
        if mapping:
            mapping["room_at_match"] = actual_room or p.target_room
            self.store.upsert(mapping)

    # --- Phase 3: rename HomeKit to match HA friendly name (writes, gated) ---
    async def build_rename_plan(self) -> RenamePlan:
        await self._refresh_hk()
        ent_by_id = {e.entity_id: e for e in self.entities}
        return build_rename_plan(self.store.all(), ent_by_id, self.hk_by_uuid)

    async def rename_dry_run(self, plans: list[PlannedRename]) -> list[dict]:
        return [await self.hk.rename(p.uuid, p.target_name, dry_run=True) for p in plans]

    async def rename_apply(self, plans: list[PlannedRename]) -> list[dict]:
        return [await self.hk.rename(p.uuid, p.target_name, dry_run=False) for p in plans]

    async def verify_rename(
        self, plans: list[PlannedRename]
    ) -> list[tuple[PlannedRename, str | None, bool]]:
        await self._refresh_hk()
        results = []
        for p in plans:
            hk = self.hk_by_uuid.get(p.uuid)
            actual = hk.name if hk else None
            ok = actual is not None and normalize_name(actual) == normalize_name(p.target_name)
            if ok:
                self._mark_renamed(p, actual)
            results.append((p, actual, ok))
        return results

    def _mark_renamed(self, p: PlannedRename, actual_name: str | None) -> None:
        mapping = self.store.get_by_entity(p.entity_id)
        if mapping:
            mapping["hk_name"] = actual_name or p.target_name
            self.store.upsert(mapping)

    async def close(self) -> None:
        try:
            await self.ha.close()
        finally:
            await self.hk.close()
