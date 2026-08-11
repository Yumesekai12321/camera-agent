import subprocess
import unittest
from unittest.mock import MagicMock, patch

import cv2
import numpy as np

from camera_agent.camera import (
    ADBCamera,
    WebcamCamera,
    WindowCamera,
    _enable_per_monitor_dpi_awareness,
)


class WebcamCameraTests(unittest.TestCase):
    @patch("camera_agent.camera.subprocess.run")
    @patch("camera_agent.camera.shutil.which", return_value=r"C:\\tools\\adb.exe")
    def test_adb_capture_decodes_full_android_png(self, which, run):
        source = np.arange(24 * 40 * 3, dtype=np.uint8).reshape(24, 40, 3)
        ok, encoded = cv2.imencode(".png", source)
        self.assertTrue(ok)
        run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=encoded.tobytes(),
            stderr=b"",
        )
        camera = ADBCamera("SERIAL-1", frames_per_second=1.0)

        frame = camera._capture_frame()

        self.assertEqual(frame.shape, source.shape)
        self.assertEqual(camera.safe_url, "adb:android-display")
        run.assert_called_once_with(
            [r"C:\\tools\\adb.exe", "-s", "SERIAL-1", "exec-out", "screencap", "-p"],
            check=False,
            capture_output=True,
            timeout=6.0,
        )
        which.assert_called_once_with("adb")

    @patch("camera_agent.camera.subprocess.run")
    @patch("camera_agent.camera.shutil.which", return_value="adb.exe")
    def test_adb_capture_rejects_portrait_when_live_view_requires_landscape(
        self, which, run
    ):
        portrait = np.full((40, 24, 3), 127, dtype=np.uint8)
        ok, encoded = cv2.imencode(".png", portrait)
        self.assertTrue(ok)
        run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=encoded.tobytes(), stderr=b""
        )
        camera = ADBCamera(require_landscape=True)

        with self.assertRaisesRegex(RuntimeError, "not landscape"):
            camera._capture_frame()

    @patch("camera_agent.camera.cv2.VideoCapture")
    def test_opens_requested_directshow_index_and_resolution(self, capture_class):
        capture = MagicMock()
        capture_class.return_value = capture
        camera = WebcamCamera(
            2,
            backend="dshow",
            width=1920,
            height=1080,
        )

        self.assertIs(camera._new_capture(), capture)
        capture_class.assert_called_once_with(2, cv2.CAP_DSHOW)
        capture.set.assert_any_call(cv2.CAP_PROP_FRAME_WIDTH, 1920)
        capture.set.assert_any_call(cv2.CAP_PROP_FRAME_HEIGHT, 1080)
        capture.set.assert_any_call(cv2.CAP_PROP_BUFFERSIZE, 1)
        self.assertNotIn("rtsp", camera.safe_url)

    def test_rejects_unknown_backend(self):
        with self.assertRaisesRegex(ValueError, "Unsupported webcam backend"):
            WebcamCamera(0, backend="unknown")

    @patch("win32gui.FindWindow", return_value=1234)
    def test_window_source_finds_exact_custom_title(self, find_window):
        camera = WindowCamera(
            "HVIP01-LIVE",
            capture_method="auto",
            frames_per_second=8,
        )
        self.assertEqual(camera._find_window(), 1234)
        find_window.assert_called_once_with(None, "HVIP01-LIVE")
        self.assertEqual(camera.safe_url, "window:HVIP01-LIVE")

    def test_window_source_rejects_missing_title(self):
        with self.assertRaisesRegex(ValueError, "cannot be empty"):
            WindowCamera("  ")

    def test_window_auto_falls_back_from_uniform_printwindow_frame(self):
        camera = WindowCamera("HVIP01-LIVE", capture_method="auto")
        black_frame = np.zeros((32, 48, 3), dtype=np.uint8)
        visible_frame = np.arange(32 * 48 * 3, dtype=np.uint8).reshape(32, 48, 3)

        with (
            patch.object(camera, "_grab_printwindow", return_value=black_frame) as printwindow,
            patch.object(camera, "_grab_screen", return_value=visible_frame) as screen,
        ):
            result = camera._grab_window(1234)
            second = camera._grab_window(1234)

        self.assertIs(result, visible_frame)
        self.assertIs(second, visible_frame)
        self.assertEqual(camera.active_capture_method, "screen")
        printwindow.assert_called_once_with(1234)
        self.assertEqual(screen.call_count, 2)

    def test_window_explicit_method_rejects_uniform_frame_as_capture_failure(self):
        camera = WindowCamera(
            "HVIP01-LIVE",
            capture_method="printwindow",
            minimum_frame_std=2.0,
        )
        with patch.object(
            camera,
            "_grab_printwindow",
            return_value=np.zeros((20, 20, 3), dtype=np.uint8),
        ):
            with self.assertRaisesRegex(RuntimeError, "uniform or black"):
                camera._grab_window(1234)

    def test_window_frame_health_check_can_be_disabled(self):
        camera = WindowCamera(
            "HVIP01-LIVE",
            capture_method="printwindow",
            minimum_frame_std=0.0,
        )
        black_frame = np.zeros((20, 20, 3), dtype=np.uint8)
        with patch.object(camera, "_grab_printwindow", return_value=black_frame):
            self.assertIs(camera._grab_window(1234), black_frame)

    @patch("camera_agent.camera.os.name", "posix")
    def test_dpi_awareness_is_a_windows_only_operation(self):
        self.assertFalse(_enable_per_monitor_dpi_awareness())


if __name__ == "__main__":
    unittest.main()
