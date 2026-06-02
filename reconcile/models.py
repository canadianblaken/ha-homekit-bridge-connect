"""Plain data models shared across the tool."""
from __future__ import annotations

import re
from dataclasses import dataclass

# LED-strip segment sub-entities, e.g. light.govee_string_lights_segment_001.
# These have no independent HomeKit accessory, so they are never mappable.
SEGMENT_RE = re.compile(r"_segment_\d+$")


def is_segment_entity(entity_id: str) -> bool:
    return bool(SEGMENT_RE.search(entity_id))


@dataclass
class Area:
    area_id: str
    name: str


@dataclass
class HAEntity:
    """A Home Assistant entity in scope (actuator or sensor) with its area."""
    entity_id: str
    friendly_name: str
    domain: str
    kind: str  # "actuator" | "sensor"
    area_id: str | None
    area_name: str | None
    is_segment: bool = False


@dataclass
class HKAccessory:
    """A HomeKit accessory from device_map."""
    uuid: str
    name: str
    room: str | None
    reachable: bool
    semantic_type: str | None = None
    category: str | None = None


@dataclass
class Match:
    entity_id: str
    uuid: str
    hk_name: str
    ha_friendly: str
    room_at_match: str | None


@dataclass
class PlannedAssignment:
    entity_id: str
    uuid: str
    hk_name: str
    current_room: str | None
    target_room: str          # exact name to send to assign_rooms
    ha_area: str | None       # HA area name (the intent)
    actionable: bool          # True = a real move to stage; False = a problem
    creates_room: bool = False
    note: str = ""


@dataclass
class MoveItem:
    entity_id: str
    friendly: str
    source_title: str | None
    source_entry: str | None
    target_title: str | None
    target_entry: str | None
    status: str          # "move" | "already" | "no_target"
    note: str = ""


@dataclass
class PlannedRename:
    entity_id: str
    uuid: str
    current_name: str | None   # current HomeKit accessory name
    target_name: str           # HA friendly name (the intent)
    actionable: bool
    note: str = ""
