"""What's in scope for matching.

Two groups:
  - actuators: things you turn on/off with a button (default ON)
  - sensors:   door / motion / water / etc. read-only triggers (opt-in, default OFF)

HomeKit characteristic sets are kept disjoint so an actuator delta is never
confused with a sensor delta.
"""
from __future__ import annotations

# --- Home Assistant entity domains ---
ACTUATOR_DOMAINS = {
    "light", "switch", "fan", "input_boolean", "button", "lock", "cover",
}
SENSOR_DOMAINS = {"binary_sensor"}
ALL_DOMAINS = ACTUATOR_DOMAINS | SENSOR_DOMAINS

# --- HomeKit characteristic names (from homekit_events) ---
# A *control* change — emitted when you actuate something.
ACTUATOR_CHARS = {
    "power", "brightness", "hue", "saturation", "color_temperature",
    "lock_target_state", "lock_current_state", "input_event",
    "target_position", "current_position",
}
# A *sensor* trip — emitted on its own; opt-in only. Deliberately excludes
# continuous readings (temperature/humidity/battery) which are pure noise.
SENSOR_CHARS = {
    "motion_detected", "contact_state", "occupancy_detected",
    "leak_detected", "smoke_detected",
    "carbon_monoxide_detected", "carbon_dioxide_detected",
}


def ha_domain(entity_id: str) -> str:
    return entity_id.split(".", 1)[0]


def ha_kind(entity_id: str) -> str | None:
    """'actuator', 'sensor', or None if the entity is out of scope entirely."""
    d = ha_domain(entity_id)
    if d in ACTUATOR_DOMAINS:
        return "actuator"
    if d in SENSOR_DOMAINS:
        return "sensor"
    return None


def active_chars(include_sensors: bool) -> set[str]:
    return ACTUATOR_CHARS | (SENSOR_CHARS if include_sensors else set())
