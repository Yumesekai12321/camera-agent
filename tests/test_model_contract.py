from pathlib import Path
import unittest

import torch


PROJECT_DIR = Path(__file__).resolve().parent.parent


class ModelContractTests(unittest.TestCase):
    def test_classifier_has_required_three_classes(self):
        checkpoint = torch.load(
            PROJECT_DIR / "models" / "facebook_classifier.pt",
            map_location="cpu",
            weights_only=True,
        )
        self.assertEqual(
            set(checkpoint["class_to_idx"]),
            {"facebook_active", "facebook_mention", "other"},
        )
        self.assertEqual(len(checkpoint["confusion_matrix"]), 3)

    def test_computer_detector_exists(self):
        model = PROJECT_DIR / "models" / "computer_detector.pt"
        self.assertTrue(model.is_file())
        self.assertGreater(model.stat().st_size, 1_000_000)


if __name__ == "__main__":
    unittest.main()

