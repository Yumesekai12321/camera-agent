from pathlib import Path
import unittest

import numpy as np

from camera_agent.ptz import PTZMove
from camera_agent.vision import ComputerScreenDetector, Detection, screen_centering_move


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
        self.assertAlmostEqual(extraction.detection.center_x, 0.5)
        self.assertAlmostEqual(extraction.detection.center_y, 0.5)

    def test_screen_centering_moves_for_cutoffs_and_off_center(self):
        # Screen cut off on right side
        det_cut_right = Detection((300, 100, 635, 400), 0.9, 10000, center_x=0.73, center_y=0.5, cut_right=True)
        self.assertEqual(screen_centering_move(det_cut_right), PTZMove.RIGHT)

        # Screen cut off on left side
        det_cut_left = Detection((5, 100, 300, 400), 0.9, 10000, center_x=0.24, center_y=0.5, cut_left=True)
        self.assertEqual(screen_centering_move(det_cut_left), PTZMove.LEFT)

        # Screen cut off at top
        det_cut_top = Detection((100, 5, 400, 300), 0.9, 10000, center_x=0.5, center_y=0.3, cut_top=True)
        self.assertEqual(screen_centering_move(det_cut_top), PTZMove.UP)

        # Screen off-center to bottom
        det_bottom = Detection((100, 350, 400, 470), 0.9, 10000, center_x=0.5, center_y=0.85)
        self.assertEqual(screen_centering_move(det_bottom), PTZMove.DOWN)

        # Screen perfectly centered inside dead zone
        det_centered = Detection((150, 150, 450, 350), 0.9, 60000, center_x=0.5, center_y=0.5)
        self.assertIsNone(screen_centering_move(det_centered))


if __name__ == "__main__":
    unittest.main()
