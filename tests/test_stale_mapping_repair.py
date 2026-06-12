import json
import tempfile
import unittest
from pathlib import Path

from reconcile.config import Config
from reconcile.core import ReconcileSession
from reconcile.models import HKAccessory


class StaleMappingRepairTest(unittest.TestCase):
    def _session(self, mappings):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "mappings.json"
        path.write_text(json.dumps({"version": 1, "mappings": mappings}))
        cfg = Config("ws://ha/api/websocket", "http://homekit/sse", None, "token")
        return ReconcileSession(cfg, store_path=path), path

    def test_repairs_missing_uuid_when_saved_name_is_unique(self):
        session, path = self._session([
            {
                "entity_id": "light.tv_room",
                "uuid": "old-uuid",
                "hk_name": "TV Room Light",
                "room_at_match": "Old Room",
            }
        ])
        session.hk_accessories = [
            HKAccessory("new-uuid", "TV Room Light", "Living Room", True)
        ]
        session.hk_by_uuid = {h.uuid: h for h in session.hk_accessories}

        session._repair_stale_mappings()

        data = json.loads(path.read_text())
        mapping = data["mappings"][0]
        self.assertEqual(mapping["uuid"], "new-uuid")
        self.assertEqual(mapping["hk_name"], "TV Room Light")
        self.assertEqual(mapping["room_at_match"], "Living Room")

    def test_leaves_missing_uuid_when_saved_name_is_ambiguous(self):
        session, path = self._session([
            {
                "entity_id": "light.duplicate",
                "uuid": "old-uuid",
                "hk_name": "Lamp",
                "room_at_match": "Old Room",
            }
        ])
        session.hk_accessories = [
            HKAccessory("new-uuid-1", "Lamp", "Office", True),
            HKAccessory("new-uuid-2", "Lamp", "Bedroom", True),
        ]
        session.hk_by_uuid = {h.uuid: h for h in session.hk_accessories}

        session._repair_stale_mappings()

        data = json.loads(path.read_text())
        self.assertEqual(data["mappings"][0]["uuid"], "old-uuid")


if __name__ == "__main__":
    unittest.main()
