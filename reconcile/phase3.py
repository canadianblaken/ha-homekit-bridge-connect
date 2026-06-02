"""Phase 3 planning: rename HomeKit accessories to match the HA friendly name.

Stage a rename only where the HomeKit name differs from the HA friendly name.
Names are compared with leading/trailing and collapsed internal whitespace
ignored, but case and apostrophe glyphs are SIGNIFICANT (those are real
display differences worth fixing). Entities with no real friendly name (where
the display name fell back to the entity_id) are skipped.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .models import HAEntity, HKAccessory, PlannedRename


def normalize_name(s: str | None) -> str:
    if not s:
        return ""
    return re.sub(r"\s+", " ", s).strip()


@dataclass
class RenamePlan:
    renames: list[PlannedRename] = field(default_factory=list)   # actionable
    problems: list[PlannedRename] = field(default_factory=list)  # not actionable
    aligned: int = 0


def build_rename_plan(
    mappings: list[dict],
    entities_by_id: dict[str, HAEntity],
    hk_by_uuid: dict[str, HKAccessory],
) -> RenamePlan:
    plan = RenamePlan()

    for m in mappings:
        eid = m.get("entity_id", "")
        uuid = m.get("uuid", "")
        ent = entities_by_id.get(eid)
        hk = hk_by_uuid.get(uuid)

        if hk is None:
            plan.problems.append(PlannedRename(
                eid, uuid, m.get("hk_name"), "", False,
                "HomeKit accessory not found in device_map"))
            continue
        if ent is None:
            plan.problems.append(PlannedRename(
                eid, uuid, hk.name, "", False, "HA entity no longer exists"))
            continue

        target = ent.friendly_name
        # Skip when there is no real friendly name (it fell back to entity_id).
        if not target or target == eid:
            plan.problems.append(PlannedRename(
                eid, uuid, hk.name, "", False,
                "HA entity has no friendly name (would be its entity_id)"))
            continue

        if normalize_name(hk.name) == normalize_name(target):
            plan.aligned += 1
            continue

        plan.renames.append(PlannedRename(eid, uuid, hk.name, target, True))

    return plan
