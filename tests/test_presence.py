from pathlib import Path
import tempfile
import unittest

from camera_agent.presence import AgentPresence


class AgentPresenceTests(unittest.TestCase):
    def test_touch_is_online_and_stale_file_is_offline(self):
        with tempfile.TemporaryDirectory() as directory:
            presence = AgentPresence(Path(directory), minimum_interval_seconds=0)
            self.assertTrue(presence.touch("yume-1", now=100.0))
            self.assertTrue(presence.is_online("yume-1", max_age_seconds=4.0))
            presence.path_for("yume-1").touch()
            self.assertTrue(presence.is_online("yume-1", max_age_seconds=4.0))
            presence.clear("yume-1")
            self.assertFalse(presence.is_online("yume-1"))

    def test_invalid_id_is_rejected(self):
        presence = AgentPresence()
        with self.assertRaises(ValueError):
            presence.path_for("../secret")


if __name__ == "__main__":
    unittest.main()
