import unittest
import random

from camera_agent.auto_patrol import (
    AutoPatrol,
    AutoPatrolConfig,
    BoundedRandomSearch,
    BoundedSearchConfig,
    PatrolAction,
    PatrolPhase,
)
from camera_agent.ptz import PTZMove


class AutoPatrolTests(unittest.TestCase):
    def test_disabled_patrol_never_moves_or_changes_detection_flow(self):
        patrol = AutoPatrol(AutoPatrolConfig(enabled=False), clock=lambda: 0.0)

        self.assertEqual(patrol.observe(screen_detected=False, alarm_event=False), ())
        self.assertEqual(patrol.phase, PatrolPhase.MANUAL)

    def test_auto_moves_until_screen_then_observes_for_five_seconds(self):
        now = [0.0]
        patrol = AutoPatrol(
            AutoPatrolConfig(
                enabled=True,
                search_move_interval_seconds=1.0,
                observe_seconds=5.0,
            ),
            clock=lambda: now[0],
        )

        self.assertEqual(
            patrol.observe(screen_detected=False, alarm_event=False),
            (PatrolAction.MOVE_NEXT,),
        )
        now[0] = 0.5
        self.assertEqual(patrol.observe(screen_detected=False, alarm_event=False), ())
        now[0] = 1.0
        self.assertEqual(
            patrol.observe(screen_detected=True, alarm_event=False),
            (PatrolAction.STOP,),
        )
        self.assertEqual(patrol.phase, PatrolPhase.OBSERVING)
        now[0] = 5.9
        self.assertEqual(patrol.observe(screen_detected=True, alarm_event=False), ())
        now[0] = 6.0
        self.assertEqual(
            patrol.observe(screen_detected=True, alarm_event=False),
            (PatrolAction.MOVE_NEXT,),
        )
        self.assertEqual(patrol.phase, PatrolPhase.SEARCHING)

    def test_three_alarm_events_end_observation_even_if_facebook_persists(self):
        now = [0.0]
        patrol = AutoPatrol(
            AutoPatrolConfig(enabled=True, max_alarm_events_per_screen=3),
            clock=lambda: now[0],
        )
        patrol.observe(screen_detected=True, alarm_event=False)

        self.assertEqual(patrol.observe(screen_detected=True, alarm_event=True), ())
        self.assertEqual(patrol.alarm_events, 1)
        now[0] = 3.0
        self.assertEqual(patrol.observe(screen_detected=True, alarm_event=True), ())
        self.assertEqual(patrol.alarm_events, 2)
        now[0] = 6.0
        self.assertEqual(
            patrol.observe(screen_detected=True, alarm_event=True),
            (PatrolAction.MOVE_NEXT,),
        )
        self.assertEqual(patrol.phase, PatrolPhase.SEARCHING)
        self.assertEqual(patrol.alarm_events, 0)

    def test_server_can_toggle_auto_without_recreating_agent(self):
        patrol = AutoPatrol(AutoPatrolConfig(enabled=False), clock=lambda: 0.0)

        patrol.set_enabled(True)
        self.assertTrue(patrol.enabled)
        self.assertEqual(patrol.observe(screen_detected=False, alarm_event=False), (PatrolAction.MOVE_NEXT,))
        patrol.set_enabled(False)
        self.assertFalse(patrol.enabled)
        self.assertEqual(patrol.phase, PatrolPhase.MANUAL)

    def test_bounded_random_search_stays_within_limits(self):
        rng = random.Random(12345)
        config = BoundedSearchConfig(max_pan_steps=3, max_tilt_steps=1)
        explorer = BoundedRandomSearch(config, rng=rng)

        visited_moves = set()
        for _ in range(300):
            move = explorer.next_direction()
            visited_moves.add(move)
            pan, tilt = explorer.position
            self.assertGreaterEqual(pan, -3)
            self.assertLessEqual(pan, 3)
            self.assertGreaterEqual(tilt, -1)
            self.assertLessEqual(tilt, 1)

        # Ensure all directions were explored
        self.assertEqual(visited_moves, {PTZMove.LEFT, PTZMove.RIGHT, PTZMove.UP, PTZMove.DOWN})

        # Test reset
        explorer.reset()
        self.assertEqual(explorer.position, (0, 0))


if __name__ == "__main__":
    unittest.main()
