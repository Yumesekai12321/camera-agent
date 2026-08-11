import unittest
from pathlib import Path
import sqlite3
import tempfile
from unittest.mock import patch

import requests

from camera_agent.config import build_mainflux_destination_id
from camera_agent.decision import AgentState, Decision
from camera_agent.mainflux import MainfluxError, MainfluxPublisher, Telemetry
from camera_agent.outbox import SQLiteEventOutbox
from camera_agent.rules import RuleEvaluation, RuleStatus


class FakeResponse:
    status_code = 202

    def raise_for_status(self):
        return None


class FakeSession:
    def __init__(self):
        self.calls = []

    def post(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return FakeResponse()


class FailingSession:
    def __init__(self):
        self.calls = 0

    def post(self, *args, **kwargs):
        self.calls += 1
        raise requests.ConnectionError("offline")


class FailingOnceSession:
    def __init__(self):
        self.calls = []

    def post(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if len(self.calls) == 1:
            raise requests.ConnectionError("offline once")
        return FakeResponse()


class MainfluxTests(unittest.TestCase):
    def test_state_change_and_heartbeat(self):
        session = FakeSession()
        now = [100.0]
        publisher = MainfluxPublisher(
            enabled=True,
            url="http://mainflux/http/messages",
            thing_key="secret",
            heartbeat_seconds=30,
            session=session,
            clock=lambda: now[0],
        )
        decision = Decision(AgentState.NO_COMPUTER, False, False, 0.0, 0, 0)
        telemetry = Telemetry(decision, True)

        self.assertTrue(publisher.publish(telemetry))
        self.assertFalse(publisher.publish(telemetry))
        now[0] += 31
        self.assertTrue(publisher.publish(telemetry))
        self.assertEqual(len(session.calls), 2)
        headers = session.calls[0][1]["headers"]
        self.assertEqual(headers["Authorization"], "Thing secret")
        self.assertEqual(headers["Content-Type"], "application/senml+json")

    def test_disabled_publisher_is_noop(self):
        publisher = MainfluxPublisher(
            enabled=False,
            url="http://unused",
            thing_key=None,
        )
        decision = Decision(AgentState.CAMERA_OFFLINE, False, False, 0.0, 0, 0)
        self.assertFalse(publisher.publish(Telemetry(decision, False)))

    def test_failure_uses_retry_backoff(self):
        session = FailingSession()
        now = [100.0]
        publisher = MainfluxPublisher(
            enabled=True,
            url="http://mainflux/http/messages",
            thing_key="secret",
            retry_backoff=10,
            session=session,
            clock=lambda: now[0],
        )
        decision = Decision(AgentState.NO_COMPUTER, False, False, 0.0, 0, 0)
        with self.assertRaisesRegex(MainfluxError, "offline"):
            publisher.publish(Telemetry(decision, True))
        self.assertFalse(publisher.publish(Telemetry(decision, True)))
        self.assertEqual(session.calls, 1)
        now[0] += 11
        with self.assertRaisesRegex(MainfluxError, "offline"):
            publisher.publish(Telemetry(decision, True))
        self.assertEqual(session.calls, 2)

    def test_rule_event_is_in_payload_and_changes_signature(self):
        with tempfile.TemporaryDirectory() as directory:
            session = FakeSession()
            publisher = MainfluxPublisher(
                enabled=True,
                url="http://mainflux/http/messages",
                thing_key="secret",
                session=session,
                outbox_path=Path(directory) / "outbox.sqlite3",
                device_id="arbitrary-camera",
                destination_id="thing:arbitrary-camera",
            )
            decision = Decision(AgentState.FACEBOOK_DETECTED, True, True, 0.9, 6, 8)
            normal = Telemetry(decision, True)
            violation = Telemetry(
                decision,
                True,
                rule=RuleEvaluation(status=RuleStatus.VIOLATION, event_triggered=True),
            )
            self.assertTrue(publisher.publish(normal))
            self.assertTrue(publisher.publish(violation))
            payload = session.calls[-1][1]["json"]
            by_name = {item["n"]: item["v"] for item in payload}
            self.assertEqual(by_name["facebook_rule_status"], int(RuleStatus.VIOLATION))
            self.assertEqual(by_name["rule_violation_event"], 1)
            self.assertTrue(all(set(item) == {"n", "v", "u"} for item in payload))

    def test_edge_event_is_latched_until_mainflux_recovers(self):
        with tempfile.TemporaryDirectory() as directory:
            session = FailingOnceSession()
            now = [100.0]
            publisher = MainfluxPublisher(
                enabled=True,
                url="http://mainflux/http/messages",
                thing_key="secret",
                retry_backoff=10,
                session=session,
                clock=lambda: now[0],
                outbox_path=Path(directory) / "outbox.sqlite3",
                device_id="arbitrary-camera",
                destination_id="thing:arbitrary-camera",
            )
            decision = Decision(AgentState.FACEBOOK_DETECTED, True, True, 0.9, 6, 8)
            event = Telemetry(
                decision,
                True,
                rule=RuleEvaluation(status=RuleStatus.VIOLATION, event_triggered=True),
            )
            with self.assertRaises(MainfluxError):
                publisher.publish(event, force=True)

            now[0] += 11
            current = Telemetry(
                decision,
                True,
                rule=RuleEvaluation(status=RuleStatus.COOLDOWN),
            )
            self.assertTrue(publisher.publish(current))
            payload = session.calls[-1][1]["json"]
            by_name = {item["n"]: item["v"] for item in payload}
            self.assertEqual(by_name["rule_violation_event"], 1)

    def test_edge_event_survives_process_restart_in_sqlite_outbox(self):
        decision = Decision(AgentState.FACEBOOK_DETECTED, True, True, 0.9, 6, 8)
        event = Telemetry(
            decision,
            True,
            rule=RuleEvaluation(status=RuleStatus.VIOLATION, event_triggered=True),
        )
        with tempfile.TemporaryDirectory() as directory:
            outbox_path = Path(directory) / "outbox.sqlite3"
            failing = MainfluxPublisher(
                enabled=True,
                url="http://mainflux/http/messages",
                thing_key="secret",
                session=FailingSession(),
                outbox_path=outbox_path,
                device_id="office-01",
                destination_id="thing:office-01",
            )
            with self.assertRaises(MainfluxError):
                failing.publish(event, force=True)

            recovered_session = FakeSession()
            recovered = MainfluxPublisher(
                enabled=True,
                url="http://mainflux/http/messages",
                thing_key="secret",
                session=recovered_session,
                outbox_path=outbox_path,
                device_id="office-01",
                destination_id="thing:office-01",
            )
            current = Telemetry(
                decision,
                True,
                rule=RuleEvaluation(status=RuleStatus.COOLDOWN),
            )
            self.assertTrue(recovered.publish(current))
            payload = recovered_session.calls[-1][1]["json"]
            by_name = {item["n"]: item["v"] for item in payload}
            self.assertEqual(by_name["rule_violation_event"], 1)
            self.assertIsNone(
                SQLiteEventOutbox(
                    outbox_path,
                    "office-01",
                    "thing:office-01",
                ).peek()
            )

    def test_edge_event_is_not_posted_when_sqlite_enqueue_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            session = FakeSession()
            publisher = MainfluxPublisher(
                enabled=True,
                url="http://mainflux/http/messages",
                thing_key="secret",
                session=session,
                outbox_path=Path(directory) / "outbox.sqlite3",
                device_id="arbitrary-camera",
                destination_id="thing:arbitrary-camera",
            )
            decision = Decision(AgentState.FACEBOOK_DETECTED, True, True, 0.9, 6, 8)
            event = Telemetry(
                decision,
                True,
                rule=RuleEvaluation(status=RuleStatus.VIOLATION, event_triggered=True),
            )

            with patch.object(
                publisher.outbox,
                "enqueue",
                side_effect=sqlite3.OperationalError("database locked"),
            ):
                with self.assertRaisesRegex(MainfluxError, "outbox"):
                    publisher.publish(event, force=True)

            self.assertEqual(session.calls, [])

    def test_outbox_is_partitioned_by_device_and_destination(self):
        payload = [{"n": "rule_violation_event", "v": 1, "u": "bool"}]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "outbox.sqlite3"
            first = SQLiteEventOutbox(path, "camera-a", "thing:a")
            second_destination = SQLiteEventOutbox(path, "camera-a", "thing:b")
            second_device = SQLiteEventOutbox(path, "camera-b", "thing:a")

            first.enqueue(payload)

            self.assertEqual(first.peek().payload, payload)
            self.assertIsNone(second_destination.peek())
            self.assertIsNone(second_device.peek())

    def test_rebinding_device_preserves_and_quarantines_prior_destination_rows(self):
        payload = [{"n": "camera_offline_event", "v": 1, "u": "bool"}]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "outbox.sqlite3"
            old_binding = SQLiteEventOutbox(path, "camera-a", "thing:old")
            old_binding.enqueue(payload)

            new_binding = SQLiteEventOutbox(path, "camera-a", "thing:new")

            self.assertIsNone(new_binding.peek())
            self.assertEqual(old_binding.peek().payload, payload)
            connection = sqlite3.connect(path)
            try:
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM mainflux_events").fetchone()[0],
                    1,
                )
            finally:
                connection.close()

    def test_outbox_schema_migration_preserves_legacy_rows(self):
        payload_json = '[{"n":"camera_offline_event","v":1,"u":"bool"}]'
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "outbox.sqlite3"
            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    """
                    CREATE TABLE mainflux_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        device_id TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
                connection.execute(
                    "INSERT INTO mainflux_events(device_id, payload_json) VALUES (?, ?)",
                    ("camera-a", payload_json),
                )
                connection.commit()
            finally:
                connection.close()

            current_binding = SQLiteEventOutbox(path, "camera-a", "thing:new")
            legacy_binding = SQLiteEventOutbox(path, "camera-a", "")

            self.assertIsNone(current_binding.peek())
            self.assertIsNotNone(legacy_binding.peek())

    def test_outbox_destination_fingerprint_never_stores_raw_thing_key(self):
        raw_key = "high-entropy-mainflux-thing-key"
        destination = build_mainflux_destination_id(
            thing_id=None,
            thing_key=raw_key,
            thing_key_env="CAMERA_A_THING_KEY",
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "outbox.sqlite3"
            outbox = SQLiteEventOutbox(path, "camera-a", destination)
            outbox.enqueue([{"n": "camera_offline_event", "v": 1, "u": "bool"}])
            connection = sqlite3.connect(path)
            try:
                row = connection.execute(
                    "SELECT destination_id, payload_json FROM mainflux_events"
                ).fetchone()
            finally:
                connection.close()

        stored = " ".join(str(value) for value in row)
        self.assertTrue(destination.startswith("key-sha256:"))
        self.assertNotIn(raw_key, stored)

    def test_operational_metrics_are_in_payload(self):
        publisher = MainfluxPublisher(
            enabled=True,
            url="http://mainflux/http/messages",
            thing_key="secret",
            session=FakeSession(),
        )
        decision = Decision(AgentState.NO_COMPUTER, False, False, 0.0, 0, 0)
        payload = publisher._payload(
            Telemetry(
                decision,
                True,
                frame_age_seconds=0.25,
                inference_ms=42.5,
                rtsp_reconnect_count=3,
            )
        )
        by_name = {item["n"]: item["v"] for item in payload}
        self.assertEqual(by_name["frame_age_seconds"], 0.25)
        self.assertEqual(by_name["inference_ms"], 42.5)
        self.assertEqual(by_name["rtsp_reconnect_count"], 3)


if __name__ == "__main__":
    unittest.main()
