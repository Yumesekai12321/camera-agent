from __future__ import annotations

import argparse
from pathlib import Path
import shutil

import cv2

from camera_agent.config import PROJECT_DIR, Settings
from camera_agent.vision import ComputerScreenDetector


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}


def images_in(directory: Path):
    if not directory.exists():
        return []
    return sorted(
        path
        for path in directory.iterdir()
        if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Crop monitor screens into a 3-class dataset.")
    parser.add_argument(
        "--raw",
        type=Path,
        default=PROJECT_DIR / "data" / "facebook" / "raw",
        help="Root with facebook_active/facebook_mention/other folders.",
    )
    parser.add_argument(
        "--other-input",
        type=Path,
        default=PROJECT_DIR / "data" / "computer" / "raw",
        help="Optional extra raw images known not to show active Facebook.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=PROJECT_DIR / "data" / "facebook" / "cropped",
    )
    parser.add_argument(
        "--unresolved",
        type=Path,
        default=PROJECT_DIR / "data" / "facebook" / "unresolved",
    )
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument(
        "--confidence",
        type=float,
        default=None,
        help="Override the computer detector threshold for a recovery pass.",
    )
    args = parser.parse_args()

    settings = Settings.from_env()
    settings.validate(require_camera=False, require_mainflux=False)
    detector = ComputerScreenDetector(
        settings.computer_model,
        confidence=(
            args.confidence if args.confidence is not None else settings.computer_confidence
        ),
        image_size=settings.detector_image_size,
    )

    sources = {
        "facebook_active": args.raw / "facebook_active",
        "facebook_mention": args.raw / "facebook_mention",
        "other": args.raw / "other",
    }
    if args.other_input.exists():
        sources["other_extra"] = args.other_input

    ok = failed = 0
    for source_name, source_dir in sources.items():
        class_name = "other" if source_name == "other_extra" else source_name
        output_dir = args.output / class_name
        unresolved_dir = args.unresolved / class_name
        output_dir.mkdir(parents=True, exist_ok=True)
        unresolved_dir.mkdir(parents=True, exist_ok=True)
        paths = images_in(source_dir)
        print(f"{source_name}: {len(paths)} images")
        for index, image_path in enumerate(paths, 1):
            output_path = output_dir / image_path.name
            if output_path.exists() and not args.overwrite:
                continue
            image = cv2.imread(str(image_path))
            if image is None:
                failed += 1
                continue
            extraction = detector.extract(image)
            if extraction.screen is None:
                shutil.copy2(image_path, unresolved_dir / image_path.name)
                failed += 1
                continue
            if cv2.imwrite(str(output_path), extraction.screen):
                ok += 1
            if index % 25 == 0 or index == len(paths):
                print(f"  {index}/{len(paths)}")
    print(f"Done: cropped={ok}, unresolved={failed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
