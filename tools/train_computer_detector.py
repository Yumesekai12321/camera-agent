from __future__ import annotations

import argparse
from pathlib import Path

from ultralytics import YOLO

from camera_agent.config import PROJECT_DIR


def main() -> int:
    parser = argparse.ArgumentParser(description="Train the computer-screen detector.")
    parser.add_argument(
        "--data",
        type=Path,
        default=PROJECT_DIR / "data" / "computer" / "detector" / "data.yaml",
    )
    parser.add_argument(
        "--base-model",
        type=Path,
        default=PROJECT_DIR / "models" / "training" / "yolo26n.pt",
    )
    parser.add_argument("--minutes", type=float, default=30.0)
    parser.add_argument("--image-size", type=int, default=512)
    parser.add_argument("--batch", type=int, default=8)
    args = parser.parse_args()

    model = YOLO(str(args.base_model))
    model.train(
        data=str(args.data),
        time=args.minutes / 60.0,
        imgsz=args.image_size,
        batch=args.batch,
        fraction=0.60,
        cache="disk",
        amp=False,
        workers=2,
        project=str(PROJECT_DIR / "runs" / "computer"),
        name="computer_detector",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

