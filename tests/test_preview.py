import unittest

import numpy as np

from camera_agent.application import render_compact_status, resize_preview
from camera_agent.decision import AgentState, Decision
from camera_agent.rules import RuleEvaluation, RuleStatus
from camera_agent.vision import ScreenExtraction


class PreviewTests(unittest.TestCase):
    def test_resize_fits_complete_sixteen_by_nine_frame(self):
        frame = np.zeros((1080, 1920, 3), dtype=np.uint8)
        preview = resize_preview(frame, 1280, 720)
        self.assertEqual(preview.shape, (720, 1280, 3))

    def test_resize_preserves_wide_aspect_ratio_instead_of_cropping(self):
        frame = np.zeros((1000, 2000, 3), dtype=np.uint8)
        preview = resize_preview(frame, 1280, 720)
        self.assertEqual(preview.shape, (640, 1280, 3))

    def test_compact_status_has_fixed_small_canvas(self):
        decision = Decision(AgentState.COMPUTER_NO_FACEBOOK, True, False, 0.1, 0, 6)
        panel = render_compact_status(
            ScreenExtraction(True, np.zeros((100, 200, 3), dtype=np.uint8), None),
            decision,
            RuleEvaluation(status=RuleStatus.NORMAL),
            0.1,
            0.6,
            camera_online=True,
        )
        self.assertEqual(panel.shape, (230, 540, 3))


if __name__ == "__main__":
    unittest.main()
