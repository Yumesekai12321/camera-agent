"""Verify an operator-installed YOLO COCO Person Guard model before rollout.

This tool reads only a local artifact.  It does not download, modify or upload
the model, so deployment stays explicit and reproducible.
"""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

from camera_agent.vision import PersonDetector


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify an already-downloaded local YOLO COCO Person Guard model."
    )
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--sha256", help="Expected SHA-256. Required for --load-check.")
    parser.add_argument(
        "--load-check",
        action="store_true",
        help="Also load the local model and confirm that COCO class 0 is person.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    path = args.model.expanduser().resolve()
    if not path.is_file():
        raise SystemExit(f"Model does not exist: {path}")
    actual = sha256_file(path)
    if args.sha256 and actual.casefold() != args.sha256.strip().casefold():
        raise SystemExit("SHA-256 mismatch; do not enable Person Guard.")
    print(f"SHA256={actual}")
    if args.load_check:
        if not args.sha256:
            raise SystemExit("--load-check requires --sha256")
        detector = PersonDetector(path, expected_sha256=args.sha256)
        names = getattr(detector.model, "names", {})
        person = names.get(0) if isinstance(names, dict) else None
        if str(person).casefold() != "person":
            raise SystemExit("Model is not a COCO person-class model.")
        print("PASS: local verified model exposes COCO class 0 as person")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
