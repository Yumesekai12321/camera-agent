from http.client import HTTPConnection
import json
from pathlib import Path
import tempfile
import threading
import unittest

from camera_agent.control import CommandStore
from camera_agent.config import Settings
from camera_agent.features import FeatureId
from camera_agent.fleet_config import DeviceDefinition
from camera_agent.local_preview import LocalPreviewStore
from tools.control_server import ControlHTTPServer


class ControlServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = CommandStore(Path(self.temp.name) / "commands.sqlite3")
        self.preview_root = Path(self.temp.name) / "previews"
        settings = Settings.from_env(Path("missing.env"))
        self.devices = (
            DeviceDefinition(
                "lobby-01", "Lobby camera", settings,
                feature_allowed=frozenset({FeatureId.FACEBOOK_MONITOR, FeatureId.PERSON_GUARD}),
            ),
            DeviceDefinition("yard-01", "Yard camera", settings),
        )
        self.server = ControlHTTPServer(
            ("127.0.0.1", 0), self.store, "test-token", preview_root=self.preview_root,
            require_presence=False, devices=self.devices,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def request(self, method, path, body=None, headers=None):
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        data = json.loads(response.read())
        connection.close()
        return response.status, data

    def test_authorized_command_is_enqueued_for_only_target_agent(self):
        status, body = self.request(
            "POST",
            "/v1/devices/lobby-01/commands",
            body=json.dumps({"action": "set_auto", "payload": {"enabled": True}}),
            headers={
                "Content-Type": "application/json",
                "X-Control-Server-Token": "test-token",
            },
        )

        self.assertEqual(status, 202)
        self.assertEqual(self.store.claim("other-01"), ())
        command = self.store.claim("lobby-01")[0]
        self.assertEqual(command.command_id, body["command_id"])
        self.assertEqual(command.payload, {"enabled": True})

    def test_rejects_missing_token(self):
        status, body = self.request(
            "POST",
            "/v1/devices/lobby-01/commands",
            body=json.dumps({"action": "stop", "payload": {}}),
            headers={"Content-Type": "application/json"},
        )

        self.assertEqual(status, 401)
        self.assertEqual(body["error"], "unauthorized")

    def test_fails_fast_when_agent_heartbeat_is_missing(self):
        self.server.require_presence = True
        status, body = self.request(
            "POST",
            "/v1/devices/lobby-01/commands",
            body=json.dumps({"action": "stop", "payload": {}}),
            headers={
                "Content-Type": "application/json",
                "X-Control-Server-Token": "test-token",
            },
        )
        self.assertEqual(status, 503)
        self.assertIn("agent offline", body["error"])

    def test_preview_requires_token_and_serves_latest_jpeg(self):
        import numpy as np

        LocalPreviewStore(self.preview_root, enabled=True, minimum_interval_seconds=0).publish(
            "lobby-01", np.zeros((24, 32, 3), dtype=np.uint8), now=1.0
        )
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        connection.request("GET", "/v1/devices/lobby-01/preview")
        response = connection.getresponse()
        self.assertEqual(response.status, 401)
        response.read()
        connection.close()

        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        connection.request(
            "GET",
            "/v1/devices/lobby-01/preview",
            headers={"X-Control-Server-Token": "test-token"},
        )
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Content-Type"), "image/jpeg")
        self.assertTrue(response.read().startswith(b"\xff\xd8"))
        connection.close()

    def test_device_name_selector_and_durable_desired_state_are_token_gated(self):
        status, body = self.request("GET", "/v1/devices")
        self.assertEqual(status, 401)
        self.assertEqual(body["error"], "unauthorized")

        status, body = self.request(
            "GET", "/v1/devices", headers={"X-Control-Server-Token": "test-token"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            [device["display_name"] for device in body["devices"]],
            ["Lobby camera", "Yard camera"],
        )
        status, body = self.request(
            "POST", "/v1/devices/lobby-01/desired-state",
            body=json.dumps({"runtime_enabled": True, "feature": "person_guard"}),
            headers={"Content-Type": "application/json", "X-Control-Server-Token": "test-token"},
        )
        self.assertEqual(status, 202)
        self.assertEqual(body["generation"], 1)
        self.assertEqual(body["status"], "pending_local")
        command = self.store.claim("lobby-01")[0]
        self.assertEqual(command.action, "set_desired_state")
        self.assertEqual(
            command.payload,
            {"runtime_enabled": True, "feature": "person_guard", "generation": 1},
        )
        record = self.server.desired_store.get("lobby-01")
        self.assertEqual(record.state.feature, FeatureId.PERSON_GUARD)

        status, body = self.request(
            "POST", "/v1/devices/yard-01/desired-state",
            body=json.dumps({"runtime_enabled": True, "feature": "person_guard"}),
            headers={"Content-Type": "application/json", "X-Control-Server-Token": "test-token"},
        )
        self.assertEqual(status, 400)
        self.assertIn("not installed", body["error"])

    def test_ui_uses_session_storage_not_local_storage_or_token_url(self):
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        connection.request("GET", "/ui")
        response = connection.getresponse()
        page = response.read().decode("utf-8")
        connection.close()
        self.assertEqual(response.status, 200)
        self.assertIn("sessionStorage", page)
        self.assertNotIn("localStorage", page)
        self.assertNotIn("?token", page)
        self.assertIn("waitLocalCommand", page)
        self.assertIn("}else if(b.command_id){", page)
        self.assertIn("let outcome=await waitLocalCommand(b.command_id)", page)
        self.assertIn("sendCommand('set_auto'", page)
        self.assertNotIn("onclick=\"command(", page)


if __name__ == "__main__":
    unittest.main()
