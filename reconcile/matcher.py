"""Correlation logic for listen-mode 'add a device'.

Goal: require EXACTLY ONE changed device on each side before proposing a
match. Returns the deltas so the UI can debounce when there are too many.
"""
from __future__ import annotations

from dataclasses import dataclass

from .models import is_segment_entity
from .scope import active_chars, ha_kind


@dataclass
class Correlation:
    ha_entities: list[str]            # distinct HA entity_ids that changed
    hk_accessories: dict[str, dict]   # uuid -> accessory dict (from the event)

    @property
    def ok(self) -> bool:
        return len(self.ha_entities) == 1 and len(self.hk_accessories) == 1

    @property
    def ha_entity(self) -> str | None:
        return self.ha_entities[0] if len(self.ha_entities) == 1 else None

    @property
    def hk_uuid(self) -> str | None:
        if len(self.hk_accessories) == 1:
            return next(iter(self.hk_accessories))
        return None

    @property
    def hk_accessory(self) -> dict | None:
        if len(self.hk_accessories) == 1:
            return next(iter(self.hk_accessories.values()))
        return None


def ha_deltas(state_changed_data: list[dict], include_sensors: bool) -> list[str]:
    """Distinct in-scope entity_ids whose state (or light brightness) changed.

    Sensors are excluded unless include_sensors is set; LED-strip segments are
    always excluded (no HK accessory, and a parent toggle floods them)."""
    ids: list[str] = []
    seen: set[str] = set()
    for d in state_changed_data:
        eid = d.get("entity_id", "")
        if eid in seen:
            continue
        kind = ha_kind(eid)
        if kind is None:
            continue
        if kind == "sensor" and not include_sensors:
            continue
        if eid.startswith("light.") and is_segment_entity(eid):
            continue
        new = d.get("new_state")
        old = d.get("old_state")
        if new is None:
            continue
        changed = (
            old is None
            or old.get("state") != new.get("state")
            or old.get("attributes", {}).get("brightness")
            != new.get("attributes", {}).get("brightness")
        )
        if changed:
            seen.add(eid)
            ids.append(eid)
    return ids


def hk_deltas(events: list[dict], chars: set[str]) -> dict[str, dict]:
    """Distinct accessories that emitted an in-scope characteristic change.
    Sensor noise (temperature/humidity/battery) is dropped by characteristic
    name; whether motion/contact count depends on the active char set."""
    out: dict[str, dict] = {}
    for ev in events:
        if ev.get("type") != "characteristic_change":
            continue
        if ev.get("characteristic") not in chars:
            continue
        acc = ev.get("accessory", {})
        uid = acc.get("id")
        if not uid:
            continue
        out.setdefault(uid, acc)
    return out


def correlate(
    state_changed_data: list[dict],
    hk_events: list[dict],
    include_sensors: bool = False,
) -> Correlation:
    return Correlation(
        ha_entities=ha_deltas(state_changed_data, include_sensors),
        hk_accessories=hk_deltas(hk_events, active_chars(include_sensors)),
    )
