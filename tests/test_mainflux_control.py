import json
from pathlib import Path
import tempfile
import unittest

from camera_agent.control import CommandStore
from camera_agent.features import DesiredFeatureState, FeatureId
from camera_agent.mainflux_control import (
    DesiredStateStore,
    MainfluxAgentControl,
    MainfluxControlError,
    MainfluxControlHub,
    MQTTControlSettings,
    decode_desired_state,
    decode_typed_command,
    encode_desired_state,
    encode_typed_command,
)


class _FakeMQTT:
    def __init__(self):
        self.messages = []
        self.connected = True
    def publish(self, topic, payload, *, qos=1):
        self.messages.append((topic, payload, qos))
        return True


class MainfluxControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = MQTTControlSettings(
            host="mqtt.vpn.test", port=8883, tls=True, ca_file=None,
            subtopic_prefix="camera-agent",
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_desired_state_schema_is_exact_and_generation_is_durable(self):
        state = DesiredFeatureState(False, FeatureId.NONE, 7)
        payload = encode_desired_state("lobby-01", state)
        self.assertEqual(decode_desired_state(payload), ("lobby-01", state))
        malformed = json.dumps({"schema": 1, "device_id": "lobby-01", "generation": 1,
                                "runtime_enabled": True, "feature": "none", "code": "x"})
        with self.assertRaisesRegex(MainfluxControlError, "unsupported"):
            decode_desired_state(malformed)

        store = DesiredStateStore(Path(self.temp.name) / "desired.sqlite3")
        first = store.set_desired("lobby-01", runtime_enabled=True, feature=FeatureId.FACEBOOK_MONITOR)
        second = store.set_desired("lobby-01", runtime_enabled=False, feature=FeatureId.NONE)
        self.assertEqual((first.state.generation, second.state.generation), (1, 2))
        store.acknowledge("lobby-01", first.state, status="applied")
        self.assertIsNone(store.get("lobby-01").applied_generation)
        store.acknowledge("lobby-01", second.state, status="applied")
        self.assertEqual(store.get("lobby-01").applied_generation, 2)
        store.acknowledge("lobby-01", second.state, status="online")
        self.assertEqual(store.get("lobby-01").applied_status, "applied")

    def test_hub_saves_before_publish_resends_and_records_ack(self):
        store = DesiredStateStore(Path(self.temp.name) / "desired.sqlite3")
        hub = MainfluxControlHub(None, store, channel_by_device={"lobby-01": "control-lobby-01"})
        fake = _FakeMQTT()
        hub.settings = self.settings
        hub._mqtt = fake

        record = hub.set_desired("lobby-01", runtime_enabled=True, feature=FeatureId.PERSON_GUARD)
        self.assertEqual(record.state.generation, 1)
        self.assertEqual(store.get("lobby-01").state.feature, FeatureId.PERSON_GUARD)
        self.assertEqual(fake.messages[0][0], self.settings.desired_topic("control-lobby-01", "lobby-01"))
        self.assertEqual(hub.resend_all(), 1)
        ack = {
            "schema": 1, "device_id": "lobby-01", "generation": 1,
            "runtime_enabled": True, "feature": "person_guard", "status": "applied",
        }
        hub._on_status(self.settings.status_topic("control-lobby-01", "lobby-01"), json.dumps(ack).encode())
        self.assertEqual(store.get("lobby-01").applied_status, "applied")
        self.assertIsNotNone(store.presence("lobby-01"))

    def test_agent_bridge_only_enqueues_allowlisted_messages(self):
        commands = CommandStore(Path(self.temp.name) / "commands.sqlite3")
        bridge = MainfluxAgentControl(
            None, device_id="lobby-01", thing_id=None, thing_key=None,
            control_channel_id=None, command_store=commands
        )
        bridge.settings = self.settings
        bridge.control_channel_id = "control-lobby-01"
        state = DesiredFeatureState(True, FeatureId.PERSON_GUARD, 4)
        bridge._on_desired(self.settings.desired_topic("control-lobby-01", "lobby-01"), encode_desired_state("lobby-01", state))
        queued = commands.claim("lobby-01")
        self.assertEqual(queued[0].action, "set_desired_state")
        self.assertEqual(queued[0].payload["generation"], 4)
        payload = encode_typed_command("lobby-01", "stop", {})
        self.assertEqual(decode_typed_command(payload), ("lobby-01", "stop", {}))
        bridge._on_desired(self.settings.command_topic("control-lobby-01", "lobby-01"), payload)
        self.assertEqual(commands.claim("lobby-01")[0].action, "stop")

    def test_agent_bridge_records_local_ack_when_mqtt_is_not_configured(self):
        commands = CommandStore(Path(self.temp.name) / "commands.sqlite3")
        desired = DesiredStateStore(Path(self.temp.name) / "desired.sqlite3")
        bridge = MainfluxAgentControl(
            None,
            device_id="lobby-01",
            thing_id=None,
            thing_key=None,
            control_channel_id=None,
            command_store=commands,
            desired_store=desired,
        )
        state = desired.set_desired(
            "lobby-01", runtime_enabled=True, feature=FeatureId.PERSON_GUARD
        ).state

        bridge.publish_ack("lobby-01", state, "applied", None)

        record = desired.get("lobby-01")
        self.assertEqual(record.applied_generation, state.generation)
        self.assertEqual(record.applied_status, "applied")
        self.assertIsNotNone(desired.presence("lobby-01"))

    def test_agent_bridge_replays_local_desired_state_after_restart(self):
        commands = CommandStore(Path(self.temp.name) / "commands.sqlite3")
        desired = DesiredStateStore(Path(self.temp.name) / "desired.sqlite3")
        state = desired.set_desired(
            "lobby-01", runtime_enabled=True, feature=FeatureId.PERSON_GUARD
        ).state
        bridge = MainfluxAgentControl(
            None,
            device_id="lobby-01",
            thing_id=None,
            thing_key=None,
            control_channel_id=None,
            command_store=commands,
            desired_store=desired,
        )

        bridge.start()

        command = commands.claim("lobby-01")[0]
        self.assertEqual(command.action, "set_desired_state")
        self.assertEqual(command.payload["generation"], state.generation)
        self.assertEqual(command.payload["feature"], "person_guard")


if __name__ == "__main__":
    unittest.main()
