"""Persisted confirmed mappings (entity_id <-> uuid) in mappings.json."""
from __future__ import annotations

import json
from pathlib import Path


class MappingStore:
    """entity_id is the source of truth (one HA light -> one HK accessory)."""

    def __init__(self, path: str | Path = "mappings.json"):
        self.path = Path(path)
        self._by_entity: dict[str, dict] = {}
        self.load()

    def load(self) -> None:
        self._by_entity.clear()
        if self.path.exists():
            data = json.loads(self.path.read_text() or "{}")
            for m in data.get("mappings", []):
                if "entity_id" in m and "uuid" in m:
                    self._by_entity[m["entity_id"]] = m

    @property
    def _by_uuid(self) -> dict[str, dict]:
        return {m["uuid"]: m for m in self._by_entity.values()}

    def get_by_entity(self, entity_id: str) -> dict | None:
        return self._by_entity.get(entity_id)

    def get_by_uuid(self, uuid: str) -> dict | None:
        return self._by_uuid.get(uuid)

    def all(self) -> list[dict]:
        return list(self._by_entity.values())

    def upsert(self, mapping: dict) -> None:
        self._by_entity[mapping["entity_id"]] = mapping
        self.save()

    def save(self) -> None:
        data = {"version": 1, "mappings": list(self._by_entity.values())}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(self.path)
