from pathlib import Path
import unittest

from camera_agent.decision import AgentState, Decision
from camera_agent.rules import (
    CameraOfflineRuleConfig,
    FacebookRuleConfig,
    MonitoringRuleEngine,
    RuleConfigurationError,
    RuleStatus,
    RulesConfig,
)


PROJECT_DIR = Path(__file__).resolve().parent.parent


class RuleEngineTests(unittest.TestCase):
    def setUp(self):
        self.now = [100.0]
        self.config = RulesConfig(
            facebook=FacebookRuleConfig(
                minimum_confidence=0.6,
                trigger_after_seconds=0.0,
                cooldown_seconds=2.0,
            ),
            camera_offline=CameraOfflineRuleConfig(),
        )
        self.engine = MonitoringRuleEngine(
            self.config,
            clock=lambda: self.now[0],
        )

    @staticmethod
    def decision(state, score=0.0):
        return Decision(
            state=state,
            computer_detected=state in {AgentState.COMPUTER_NO_FACEBOOK, AgentState.FACEBOOK_DETECTED},
            facebook_active=state == AgentState.FACEBOOK_DETECTED,
            active_score=score,
            votes=6,
            samples=8,
        )

    def test_facebook_rule_pending_trigger_cooldown_and_clear(self):
        result = self.engine.update(self.decision(AgentState.FACEBOOK_DETECTED, 0.9))
        self.assertTrue(result.event_triggered)
        self.assertEqual(result.status, RuleStatus.VIOLATION)
        result = self.engine.update(self.decision(AgentState.FACEBOOK_DETECTED, 0.9))
        self.assertEqual(result.status, RuleStatus.COOLDOWN)
        result = self.engine.update(self.decision(AgentState.COMPUTER_NO_FACEBOOK))
        self.assertTrue(result.event_cleared)

    def test_non_facebook_state_does_not_start_rule(self):
        result = self.engine.update(self.decision(AgentState.COMPUTER_NO_FACEBOOK, 0.5))
        self.assertEqual(result.status, RuleStatus.NORMAL)

    def test_camera_offline_and_recovery_events(self):
        result = self.engine.update(self.decision(AgentState.CAMERA_OFFLINE))
        self.assertTrue(result.camera_offline_event)
        self.assertFalse(self.engine.update(self.decision(AgentState.CAMERA_OFFLINE)).camera_offline_event)
        result = self.engine.update(self.decision(AgentState.NO_COMPUTER))
        self.assertTrue(result.camera_recovered_event)

    def test_rule_overrides_create_an_independent_effective_config(self):
        base = RulesConfig.from_toml(PROJECT_DIR / "config" / "rules.toml")
        original_confidence = base.facebook.minimum_confidence
        original_cooldown = base.facebook.cooldown_seconds
        effective = base.with_overrides(
            {
                "facebook_usage": {
                    "minimum_confidence": 0.81,
                    "cooldown_seconds": 15,
                },
                "camera_offline": {"trigger_after_seconds": 4},
            }
        )

        self.assertEqual(base.facebook.minimum_confidence, original_confidence)
        self.assertEqual(base.facebook.cooldown_seconds, original_cooldown)
        self.assertEqual(effective.facebook.minimum_confidence, 0.81)
        self.assertEqual(effective.facebook.cooldown_seconds, 15.0)
        self.assertEqual(effective.camera_offline.trigger_after_seconds, 4.0)

    def test_rule_overrides_reject_unknown_sections_and_fields(self):
        base = RulesConfig.from_toml(PROJECT_DIR / "config" / "rules.toml")
        with self.assertRaisesRegex(RuleConfigurationError, "unknown rule section"):
            base.with_overrides({"office_01": {"cooldown_seconds": 5}})
        with self.assertRaisesRegex(RuleConfigurationError, "unknown rule override"):
            base.with_overrides({"facebook_usage": {"alarm_level": 5}})


if __name__ == "__main__":
    unittest.main()
