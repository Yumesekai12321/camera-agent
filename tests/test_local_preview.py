import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

import numpy as np

from camera_agent.local_preview import LocalPreviewStore


class LocalPreviewTests(unittest.TestCase):
    def test_latest_frame_is_fit_without_stretch_and_can_be_cleared(self):
        with tempfile.TemporaryDirectory() as directory:
            store = LocalPreviewStore(
                Path(directory), enabled=True, max_width=640, max_height=360, minimum_interval_seconds=0
            )
            frame = np.zeros((600, 1200, 3), dtype=np.uint8)
            self.assertTrue(store.publish("yume-1", frame, now=1.0))
            encoded = store.read("yume-1")
            self.assertIsNotNone(encoded)

            import cv2

            decoded = cv2.imdecode(np.frombuffer(encoded, dtype=np.uint8), cv2.IMREAD_COLOR)
            self.assertEqual(decoded.shape[:2], (320, 640))
            store.clear("yume-1")
            self.assertIsNone(store.read("yume-1"))

    def test_store_rejects_path_traversal_ids(self):
        store = LocalPreviewStore(enabled=True)
        with self.assertRaises(ValueError):
            store.path_for("../secret")

    def test_windows_reader_lock_does_not_crash_agent(self):
        with tempfile.TemporaryDirectory() as directory:
            store = LocalPreviewStore(Path(directory), enabled=True, minimum_interval_seconds=0)
            frame = np.zeros((24, 32, 3), dtype=np.uint8)
            real_replace = __import__("os").replace
            attempts = {"count": 0}

            def flaky_replace(source, target):
                attempts["count"] += 1
                if attempts["count"] < 3:
                    raise PermissionError("simulated browser reader lock")
                return real_replace(source, target)

            with patch("camera_agent.local_preview.os.replace", side_effect=flaky_replace):
                self.assertTrue(store.publish("yume-1", frame, now=1.0))
            self.assertGreaterEqual(attempts["count"], 3)

    def test_unrecoverable_reader_lock_drops_frame_instead_of_raising(self):
        with tempfile.TemporaryDirectory() as directory:
            store = LocalPreviewStore(Path(directory), enabled=True, minimum_interval_seconds=0)
            with patch(
                "camera_agent.local_preview.os.replace",
                side_effect=PermissionError("simulated browser reader lock"),
            ):
                self.assertFalse(store.publish("yume-1", np.zeros((24, 32, 3), dtype=np.uint8), now=1.0))


if __name__ == "__main__":
    unittest.main()
