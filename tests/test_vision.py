from pathlib import Path
import unittest

import numpy as np

from camera_agent.vision import ComputerScreenDetector


class ComputerScreenDetectorTests(unittest.TestCase):
    def test_static_roi_keeps_every_selected_pixel(self):
        detector = ComputerScreenDetector(
            Path("models/computer_detector.pt"),
            normalized_roi=(0.25, 0.25, 0.5, 0.5),
        )
        frame = np.zeros((100, 200, 3), dtype=np.uint8)

        extraction = detector.extract(frame)

        self.assertTrue(extraction.computer_detected)
        self.assertTrue(extraction.used_static_roi)
        self.assertEqual(extraction.detection.box, (50, 25, 150, 75))
        self.assertEqual(extraction.screen.shape, (50, 100, 3))


if __name__ == "__main__":
    unittest.main()
