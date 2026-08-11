from __future__ import annotations

import argparse
from pathlib import Path

import cv2
import numpy as np

from camera_agent.config import PROJECT_DIR
from camera_agent.vision import FacebookClassifier


CLASSES = ("facebook_active", "facebook_mention", "other")
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Evaluate the classifier on a separate labeled dataset (never its train set)."
    )
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument(
        "--model",
        type=Path,
        default=PROJECT_DIR / "models" / "facebook_classifier.pt",
    )
    parser.add_argument("--active-threshold", type=float, default=0.72)
    parser.add_argument("--minimum-margin", type=float, default=0.20)
    parser.add_argument("--minimum-accuracy", type=float, default=0.90)
    parser.add_argument("--minimum-active-precision", type=float, default=0.95)
    parser.add_argument("--minimum-active-recall", type=float, default=0.80)
    parser.add_argument("--threads", type=int, default=2)
    args = parser.parse_args()

    classifier = FacebookClassifier(args.model, torch_threads=args.threads)
    class_to_index = {name: index for index, name in enumerate(CLASSES)}
    confusion = np.zeros((len(CLASSES), len(CLASSES)), dtype=int)
    rule_true_positive = rule_false_positive = 0
    rule_true_negative = rule_false_negative = 0
    invalid = 0

    for true_class in CLASSES:
        directory = args.dataset / true_class
        paths = sorted(
            path
            for path in directory.glob("*")
            if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS
        )
        if not paths:
            raise RuntimeError(f"No images found for {true_class}: {directory}")
        for path in paths:
            image = cv2.imread(str(path))
            if image is None:
                invalid += 1
                continue
            probabilities = classifier.predict(image)
            predicted_class = max(probabilities, key=probabilities.get)
            confusion[class_to_index[true_class], class_to_index[predicted_class]] += 1

            active = probabilities.get("facebook_active", 0.0)
            runner_up = max(
                (score for name, score in probabilities.items() if name != "facebook_active"),
                default=0.0,
            )
            rule_detected = bool(
                predicted_class == "facebook_active"
                and active >= args.active_threshold
                and active - runner_up >= args.minimum_margin
            )
            actually_active = true_class == "facebook_active"
            if rule_detected and actually_active:
                rule_true_positive += 1
            elif rule_detected:
                rule_false_positive += 1
            elif actually_active:
                rule_false_negative += 1
            else:
                rule_true_negative += 1

    total = int(confusion.sum())
    correct = int(np.trace(confusion))
    print("Dataset:", args.dataset.resolve())
    print("Images:", total, "invalid:", invalid)
    print("Confusion matrix (rows=true, columns=predicted):")
    print("classes:", CLASSES)
    print(confusion)
    accuracy = correct / max(total, 1)
    print(f"3-class accuracy: {accuracy:.3%}")
    precision = rule_true_positive / max(rule_true_positive + rule_false_positive, 1)
    recall = rule_true_positive / max(rule_true_positive + rule_false_negative, 1)
    print(
        "Rule gate: "
        f"TP={rule_true_positive} FP={rule_false_positive} "
        f"TN={rule_true_negative} FN={rule_false_negative}"
    )
    print(f"Active precision: {precision:.3%}; active recall: {recall:.3%}")
    passed = bool(
        accuracy >= args.minimum_accuracy
        and precision >= args.minimum_active_precision
        and recall >= args.minimum_active_recall
    )
    print(
        "Acceptance gate: "
        f"accuracy>={args.minimum_accuracy:.0%}, "
        f"precision>={args.minimum_active_precision:.0%}, "
        f"recall>={args.minimum_active_recall:.0%} -> "
        f"{'PASS' if passed else 'FAIL'}"
    )
    return 0 if passed else 3


if __name__ == "__main__":
    raise SystemExit(main())
