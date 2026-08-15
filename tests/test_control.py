from pathlib import Path
import tempfile
import unittest

from camera_agent.control import CommandStore, ControlCommandError


class CommandStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = CommandStore(Path(self.temp.name) / "control.sqlite3")

    def tearDown(self):
        self.temp.cleanup()

    def test_commands_are_per_device_and_claimed_once(self):
        command_id = self.store.enqueue("lobby-01", "move", {"direction": "left"})
        self.store.enqueue("other-01", "set_auto", {"enabled": True})

        commands = self.store.claim("lobby-01")

        self.assertEqual([command.command_id for command in commands], [command_id])
        self.assertEqual(commands[0].action, "move")
        self.assertEqual(self.store.claim("lobby-01"), ())
        self.store.complete(command_id, "executed", "moved left")
        self.assertEqual(self.store.get(command_id).status, "executed")

    def test_rejects_unknown_action_and_unsafe_payload(self):
        with self.assertRaisesRegex(ControlCommandError, "action"):
            self.store.enqueue("lobby-01", "shell", {"command": "whoami"})
        with self.assertRaisesRegex(ControlCommandError, "direction"):
            self.store.enqueue("lobby-01", "move", {"direction": "northwest"})

    def test_old_motion_commands_expire_before_agent_reconnect(self):
        command_id = self.store.enqueue("lobby-01", "move", {"direction": "right"})
        connection = self.store._connect()
        try:
            connection.execute(
                "UPDATE control_commands SET created_at = ? WHERE command_id = ?",
                (0.0, command_id),
            )
        finally:
            connection.close()
        self.assertEqual(self.store.claim("lobby-01", max_age_seconds=1), ())
        status = self.store.get(command_id)
        self.assertEqual(status.status, "rejected")
        self.assertEqual(status.detail, "expired")


if __name__ == "__main__":
    unittest.main()
