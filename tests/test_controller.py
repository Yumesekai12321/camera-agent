import unittest

from extras.windows_enforcement.controller import find_agent_state


class ControllerPayloadTests(unittest.TestCase):
    def test_finds_state_in_supported_payloads(self):
        self.assertEqual(find_agent_state([{"name": "agent_state", "value": 2}]), 2)
        self.assertEqual(find_agent_state({"payload": {"agent_state": 3}}), 3)
        self.assertEqual(find_agent_state('{"agent_state": 1}'), 1)

    def test_missing_or_invalid_state_is_ignored(self):
        self.assertIsNone(find_agent_state({"name": "camera_online", "value": 1}))
        self.assertIsNone(find_agent_state({"agent_state": "invalid"}))


if __name__ == "__main__":
    unittest.main()

