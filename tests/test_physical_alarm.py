from dataclasses import replace
from pathlib import Path
import time
import unittest
from unittest.mock import MagicMock

import numpy as np

from camera_agent.application import CameraAgent
from camera_agent.camera import FramePacket
from camera_agent.config import Settings
from camera_agent.decision import AgentState
from camera_agent.physical_alarm import PhysicalAlarmError, TapoSirenAlarm, build_tapo_alarm
from camera_agent.rules import CameraOfflineRuleConfig, FacebookRuleConfig, RulesConfig
from camera_agent.vision import Detection, ScreenExtraction


class FakeCamera:
    safe_url = "fake://camera"
    last_error = None
    reconnect_count = 0

    def __init__(self) -> None:
        self._sent = False

    @property
    def seconds_since_frame(self) -> float:
        return 0.0

    def start(self):
        return self

    def stop(self) -> None:
        return None

    def read_latest(self, *, after_sequence: int = -1, timeout: float = 1.0):
        if self._sent:
            return None
        self._sent = True
        frame = np.zeros((12, 12, 3), dtype=np.uint8)
        return FramePacket(frame=frame, sequence=1, captured_at=time.monotonic())


class FakePhysicalAlarm:
    def __init__(self) -> None:
        self.reasons: list[str] = []

    def trigger(self, reason: str) -> None:
        self.reasons.append(reason)


class PhysicalAlarmTests(unittest.TestCase):
    def test_tapo_alarm_validates_cooldown(self):
        with self.assertRaisesRegex(PhysicalAlarmError, "cooldown"):
            TapoSirenAlarm(
                host="camera.example.test",
                username="user",
                password="secret",
                duration_seconds=5,
                cooldown_seconds=1,
            )

    def test_factory_requires_structured_credentials_when_enabled(self):
        with self.assertRaisesRegex(PhysicalAlarmError, "host, username and password"):
            build_tapo_alarm(
                enabled=True,
                host=None,
                username="user",
                password="secret",
                duration_seconds=2,
                cooldown_seconds=10,
            )

    def test_camera_agent_triggers_physical_alarm_on_facebook_rule_event(self):
        settings = replace(
            Settings.from_env(Path("missing.env")),
            preview=False,
            preview_mode="off",
            mainflux_enabled=False,
            mainflux_thing_key=None,
            process_interval=0.05,
            history_size=2,
            minimum_history=1,
            minimum_active_frames=1,
            active_frame_threshold=0.1,
            active_min_margin=0.1,
            activate_average=0.1,
            deactivate_average=0.05,
        )
        detector = MagicMock()
        detector.extract.return_value = ScreenExtraction(
            True,
            np.zeros((8, 8, 3), dtype=np.uint8),
            Detection((0, 0, 8, 8), 0.9, 64),
        )
        classifier = MagicMock()
        classifier.degraded = False
        classifier.model_version = "test"
        classifier.predict.return_value = {
            "facebook_active": 0.95,
            "facebook_mention": 0.03,
            "other": 0.02,
        }
        alarm = FakePhysicalAlarm()
        rules = RulesConfig(
            facebook=FacebookRuleConfig(
                enabled=True,
                minimum_confidence=0.6,
                trigger_after_seconds=0,
                cooldown_seconds=10,
            ),
            camera_offline=CameraOfflineRuleConfig(enabled=True, trigger_after_seconds=0),
        )

        agent = CameraAgent(
            settings,
            dry_run=True,
            detector=detector,
            classifier=classifier,
            camera=FakeCamera(),
            rules_config=rules,
            physical_alarm=alarm,
        )
        agent.run(max_cycles=1)

        self.assertEqual(alarm.reasons, ["facebook_detected"])
        self.assertEqual(agent._last_displayed_state, AgentState.FACEBOOK_DETECTED)


if __name__ == "__main__":
    unittest.main()
