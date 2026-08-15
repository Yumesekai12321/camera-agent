from dataclasses import replace
import os
from pathlib import Path
import time
import unittest
from unittest.mock import MagicMock, patch

from camera_agent.application import AgentSnapshot
from camera_agent.config import Settings
from camera_agent.decision import AgentState, Decision
from camera_agent.fleet import (
    FLEET_HEADER_HEIGHT,
    FLEET_ROW_HEIGHT,
    FLEET_WIDTH,
    FleetAgent,
    FleetStatusBoard,
)
from camera_agent.fleet_config import DeviceDefinition, FleetConfig
from camera_agent.rules import RuleEvaluation, RuleStatus
from camera_agent.vision import ScreenExtraction


class FleetStatusBoardTests(unittest.TestCase):
    def setUp(self):
        with patch.dict(os.environ, {}, clear=True):
            settings = replace(
                Settings.from_env(Path("missing.env")),
                preview=False,
                preview_mode="off",
                mainflux_enabled=False,
                mainflux_thing_key=None,
                monitor_roi=(0.0, 0.0, 1.0, 1.0),
            )
        self.devices = (
            DeviceDefinition("office-01", "Office 01", settings),
            DeviceDefinition("office-02", "Office 02", settings),
        )

    def test_renders_one_row_per_device_without_camera_pixels(self):
        board = FleetStatusBoard(self.devices)
        image = board.render(now=100.0)
        self.assertEqual(
            image.shape,
            (FLEET_HEADER_HEIGHT + FLEET_ROW_HEIGHT * 2, FLEET_WIDTH, 3),
        )
        self.assertLessEqual(FLEET_WIDTH, 680)

    def test_accepts_independent_device_snapshot(self):
        board = FleetStatusBoard(self.devices)
        board.update(
            AgentSnapshot(
                device_id="office-02",
                device_name="Office 02",
                extraction=ScreenExtraction(False, None, None),
                decision=Decision(
                    AgentState.CAMERA_OFFLINE,
                    False,
                    False,
                    0.0,
                    0,
                    0,
                ),
                rule=RuleEvaluation(status=RuleStatus.NORMAL),
                raw_active_score=0.0,
                classifier_margin=0.0,
                camera_online=False,
                updated_at=time.monotonic(),
            )
        )
        image = board.render()
        self.assertGreater(int(image.sum()), 0)

    @patch("camera_agent.fleet.build_device_camera", return_value=MagicMock())
    @patch("camera_agent.fleet.ComputerScreenDetector")
    @patch("camera_agent.fleet.FacebookClassifier")
    def test_fleet_uses_one_lock_for_yolo_and_classifier_inference(
        self,
        classifier_class,
        detector_class,
        build_camera,
    ):
        config = FleetConfig(self.devices, preview=False)

        fleet = FleetAgent(config, dry_run=True)

        self.assertEqual(classifier_class.call_count, 1)
        shared_lock = classifier_class.call_args.kwargs["prediction_lock"]
        self.assertEqual(detector_class.call_count, len(self.devices))
        self.assertTrue(
            all(
                call.kwargs["prediction_lock"] is shared_lock
                for call in detector_class.call_args_list
            )
        )
        self.assertEqual(build_camera.call_count, len(self.devices))
        self.assertEqual(len(fleet.agents), len(self.devices))

    @patch("camera_agent.fleet.build_device_camera", return_value=MagicMock())
    @patch("camera_agent.fleet.ComputerScreenDetector")
    @patch("camera_agent.fleet.FacebookClassifier")
    def test_fleet_keeps_per_device_state_rule_and_publisher_instances(
        self,
        classifier_class,
        detector_class,
        build_camera,
    ):
        fleet = FleetAgent(FleetConfig(self.devices, preview=False), dry_run=True)

        first, second = fleet.agents
        self.assertIsNot(first.decision, second.decision)
        self.assertIsNot(first.rule_engine, second.rule_engine)
        self.assertIsNot(first.publisher, second.publisher)

    @patch("camera_agent.fleet.PentestAgent")
    @patch("camera_agent.fleet.FacebookClassifier")
    def test_pentest_device_does_not_load_camera_models(self, classifier_class, pentest_class):
        settings = replace(
            Settings.from_env(Path("missing.env")),
            preview=False,
            preview_mode="off",
            mainflux_enabled=False,
            mainflux_thing_key=None,
            monitor_roi=(0.0, 0.0, 1.0, 1.0),
        )
        device = DeviceDefinition(
            "security-agent-01",
            "Security assessment agent",
            settings,
            source_type="pentest",
            agent_type="pentest",
            pentest_target_host="127.0.0.1",
            pentest_ports=(80, 443),
        )

        fleet = FleetAgent(FleetConfig((device,), preview=False), dry_run=True)

        classifier_class.assert_not_called()
        pentest_class.assert_called_once()
        self.assertEqual(len(fleet.agents), 1)

    @patch("camera_agent.fleet.PentestAgent")
    @patch("camera_agent.fleet.FacebookClassifier")
    def test_tapo_backed_pentest_uses_camera_source_without_loading_facebook_models(
        self,
        classifier_class,
        pentest_class,
    ):
        settings = replace(
            Settings.from_env(Path("missing.env")),
            preview=False,
            preview_mode="off",
            mainflux_enabled=False,
            mainflux_thing_key=None,
            monitor_roi=(0.0, 0.0, 1.0, 1.0),
        )
        device = DeviceDefinition(
            "tapo-security-agent",
            "Tapo security agent",
            settings,
            source_type="rtsp",
            agent_type="pentest",
            pentest_target_host="192.168.10.20",
            pentest_ports=(554,),
        )

        fleet = FleetAgent(FleetConfig((device,), preview=False), dry_run=True)

        classifier_class.assert_not_called()
        pentest_class.assert_called_once()
        self.assertIs(pentest_class.call_args.args[0], device)
        self.assertEqual(len(fleet.agents), 1)


if __name__ == "__main__":
    unittest.main()
