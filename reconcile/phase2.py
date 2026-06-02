"""Phase 2 planning: turn confirmed mappings into room assignments.

Stage an assignment only where the HomeKit room differs from the HA area.
Room names are normalized (apostrophe glyphs, whitespace, case) so a cosmetic
difference is treated as aligned and we never spawn a duplicate room. When an
existing HomeKit room normalizes to the HA area name, reuse that room's exact
spelling as the target.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from .models import HAEntity, HKAccessory, PlannedAssignment


def normalize_room(s: str | None) -> str:
    if not s:
        return ""
    s = s.replace("’", "'").replace("‘", "'")
    s = unicodedata.normalize("NFKC", s)
    s = re.sub(r"\s+", " ", s).strip().casefold()
    return s


@dataclass
class Plan:
    assignments: list[PlannedAssignment] = field(default_factory=list)  # actionable
    problems: list[PlannedAssignment] = field(default_factory=list)     # not actionable
    aligned: int = 0


def build_plan(
    mappings: list[dict],
    entities_by_id: dict[str, HAEntity],
    hk_by_uuid: dict[str, HKAccessory],
    existing_rooms: list[str],
) -> Plan:
    norm_existing = {normalize_room(r): r for r in existing_rooms if r}
    plan = Plan()

    for m in mappings:
        eid = m.get("entity_id", "")
        uuid = m.get("uuid", "")
        hk_name = m.get("hk_name", "") or (hk_by_uuid.get(uuid).name if uuid in hk_by_uuid else "")
        ent = entities_by_id.get(eid)
        hk = hk_by_uuid.get(uuid)

        if hk is None:
            plan.problems.append(PlannedAssignment(
                eid, uuid, hk_name, None, "", ent.area_name if ent else None,
                actionable=False, note="HomeKit accessory not found in device_map"))
            continue
        if ent is None:
            plan.problems.append(PlannedAssignment(
                eid, uuid, hk_name, hk.room, "", None,
                actionable=False, note="HA entity no longer exists"))
            continue
        if not ent.area_name:
            plan.problems.append(PlannedAssignment(
                eid, uuid, hk_name, hk.room, "", None,
                actionable=False, note="HA entity has no area assigned"))
            continue

        ha_area = ent.area_name
        # Reuse an existing HK room's exact spelling if it matches the HA area.
        existing = norm_existing.get(normalize_room(ha_area))
        target = existing or ha_area
        creates = existing is None

        if normalize_room(hk.room) == normalize_room(target):
            plan.aligned += 1
            continue

        plan.assignments.append(PlannedAssignment(
            eid, uuid, hk.name or hk_name, hk.room, target, ha_area,
            actionable=True, creates_room=creates,
            note="creates new room" if creates else ""))

    return plan
