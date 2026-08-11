import unittest

from camera_agent.decision import AgentState, DecisionEngine


class DecisionEngineTests(unittest.TestCase):
    def make_engine(self):
        return DecisionEngine(
            history_size=4,
            minimum_history=3,
            minimum_active_frames=3,
            active_frame_threshold=0.6,
            activate_average=0.7,
            deactivate_average=0.3,
        )

    def test_requires_temporal_confirmation(self):
        engine = self.make_engine()
        self.assertEqual(
            engine.update(computer_detected=True, active_score=0.9).state,
            AgentState.COMPUTER_NO_FACEBOOK,
        )
        engine.update(computer_detected=True, active_score=0.8)
        result = engine.update(computer_detected=True, active_score=0.85)
        self.assertEqual(result.state, AgentState.FACEBOOK_DETECTED)
        self.assertEqual(result.votes, 3)

    def test_hysteresis_deactivates_after_low_window(self):
        engine = self.make_engine()
        for _ in range(3):
            result = engine.update(computer_detected=True, active_score=0.9)
        self.assertTrue(result.facebook_active)
        for _ in range(4):
            result = engine.update(computer_detected=True, active_score=0.1)
        self.assertEqual(result.state, AgentState.COMPUTER_NO_FACEBOOK)

    def test_no_computer_and_offline_reset_history(self):
        engine = self.make_engine()
        engine.update(computer_detected=True, active_score=0.9)
        result = engine.update(computer_detected=False)
        self.assertEqual(result.state, AgentState.NO_COMPUTER)
        self.assertEqual(result.samples, 0)
        self.assertEqual(engine.camera_offline().state, AgentState.CAMERA_OFFLINE)


if __name__ == "__main__":
    unittest.main()

