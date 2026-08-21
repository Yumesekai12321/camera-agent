import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from camera_agent.cyber.config import CyberConfig, LoggingSettings, ScopeSettings
from camera_agent.cyber.models import CameraEvent
from camera_agent.cyber.orchestrator import CyberOrchestrator


class CyberOrchestratorTests(unittest.TestCase):
    def test_dry_run_has_explicit_states_and_no_network(self):
        with tempfile.TemporaryDirectory() as directory:
            config = CyberConfig(
                mode="dry-run",
                scope=ScopeSettings(allowed_hosts=("192.168.56.20",)),
                logging=LoggingSettings(file=Path(directory) / "cyber.jsonl"),
            )
            report = CyberOrchestrator.from_config(config).process(
                CameraEvent("agent", "camera", "router")
            )
        self.assertEqual(report.state, "DONE")
        self.assertEqual(report.state_history[0], "IDLE")
        self.assertIn("PUBLISHING", report.state_history)
        self.assertTrue(report.dry_run)

    def test_outside_scope_is_denied_before_pipeline(self):
        config = CyberConfig(scope=ScopeSettings(allowed_hosts=("192.168.56.20",)))
        report = CyberOrchestrator.from_config(config).process(
            CameraEvent("agent", "camera", target_ip="8.8.8.8")
        )
        self.assertEqual(report.state, "ERROR")
        self.assertIn("DENIED_OUTSIDE_SCOPE", report.errors)


if __name__ == "__main__":
    unittest.main()
