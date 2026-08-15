import unittest
import threading
import time
from unittest.mock import MagicMock, patch

from camera_agent.ptz import OnvifPTZ, PTZError, PTZMove


class OnvifPTZTests(unittest.TestCase):
    def test_rejects_invalid_velocity_and_duration(self):
        with self.assertRaisesRegex(PTZError, "velocity"):
            OnvifPTZ("camera.example.test", 2020, "user", "pass", velocity=1.2)
        with self.assertRaisesRegex(PTZError, "duration"):
            OnvifPTZ("camera.example.test", 2020, "user", "pass", move_duration_seconds=0)

    def test_continuous_move_stops_in_finally(self):
        fake_camera = MagicMock()
        fake_media = MagicMock()
        profile = MagicMock()
        profile.token = "profile-1"
        profile.PTZConfiguration = MagicMock()
        fake_media.GetProfiles.return_value = [profile]
        fake_ptz = MagicMock()
        fake_camera.create_media_service.return_value = fake_media
        fake_camera.create_ptz_service.return_value = fake_ptz
        module = MagicMock(ONVIFCamera=MagicMock(return_value=fake_camera))
        ptz = OnvifPTZ("camera.example.test", 2020, "user", "pass", velocity=0.4)

        with patch("camera_agent.ptz.importlib.import_module", return_value=module):
            with patch("camera_agent.ptz.time.sleep"):
                ptz.move(PTZMove.LEFT, duration_seconds=0.5)

        fake_ptz.ContinuousMove.assert_called_once_with(
            {"ProfileToken": "profile-1", "Velocity": {"PanTilt": {"x": -0.4, "y": 0.0}}}
        )
        fake_ptz.Stop.assert_called_once_with(
            {"ProfileToken": "profile-1", "PanTilt": True, "Zoom": True}
        )

    def test_no_credentials_in_repr(self):
        ptz = OnvifPTZ("camera.example.test", 2020, "user", "secret-value")
        self.assertNotIn("secret-value", repr(ptz))

    def test_start_is_non_blocking_and_stop_interrupts_segment(self):
        fake_camera = MagicMock()
        fake_media = MagicMock()
        profile = MagicMock()
        profile.token = "profile-1"
        profile.PTZConfiguration = MagicMock()
        fake_media.GetProfiles.return_value = [profile]
        fake_ptz = MagicMock()
        fake_camera.create_media_service.return_value = fake_media
        fake_camera.create_ptz_service.return_value = fake_ptz
        module = MagicMock(ONVIFCamera=MagicMock(return_value=fake_camera))
        ptz = OnvifPTZ("camera.example.test", 2020, "user", "pass", velocity=0.4)

        started = threading.Event()
        fake_ptz.ContinuousMove.side_effect = lambda _payload: started.set()
        with patch("camera_agent.ptz.importlib.import_module", return_value=module):
            started_at = time.monotonic()
            ptz.start(PTZMove.RIGHT, duration_seconds=2.0)
            self.assertLess(time.monotonic() - started_at, 0.5)
            self.assertTrue(started.wait(1.0))
            ptz.stop()

        fake_ptz.Stop.assert_called()

    def test_seamless_continuous_motion_extension(self):
        fake_camera = MagicMock()
        fake_media = MagicMock()
        profile = MagicMock()
        profile.token = "profile-1"
        profile.PTZConfiguration = MagicMock()
        fake_media.GetProfiles.return_value = [profile]
        fake_ptz = MagicMock()
        fake_camera.create_media_service.return_value = fake_media
        fake_camera.create_ptz_service.return_value = fake_ptz
        module = MagicMock(ONVIFCamera=MagicMock(return_value=fake_camera))
        ptz = OnvifPTZ("camera.example.test", 2020, "user", "pass", velocity=0.4)

        started = threading.Event()
        fake_ptz.ContinuousMove.side_effect = lambda _payload: started.set()
        with patch("camera_agent.ptz.importlib.import_module", return_value=module):
            # Start first segment
            ptz.start(PTZMove.RIGHT, duration_seconds=1.0)
            self.assertTrue(started.wait(1.0))
            self.assertEqual(fake_ptz.ContinuousMove.call_count, 1)

            # Extend in the same direction: should not call Stop or duplicate ContinuousMove
            ptz.start(PTZMove.RIGHT, duration_seconds=1.0)
            self.assertEqual(fake_ptz.ContinuousMove.call_count, 1)
            fake_ptz.Stop.assert_not_called()

            # Change direction in flight: seamlessly sends new ContinuousMove
            ptz.start(PTZMove.UP, duration_seconds=1.0)
            time.sleep(0.05)
            self.assertEqual(fake_ptz.ContinuousMove.call_count, 2)
            fake_ptz.Stop.assert_not_called()

            # Now stop
            ptz.stop()

        fake_ptz.Stop.assert_called()


if __name__ == "__main__":
    unittest.main()
