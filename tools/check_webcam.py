from __future__ import annotations

import argparse
import logging
import time

import cv2

from camera_agent.application import resize_preview
from camera_agent.camera import FramePacket, WebcamCamera


def _open(
    index: int,
    backend: str,
    timeout: float,
) -> tuple[WebcamCamera, FramePacket | None]:
    camera = WebcamCamera(
        index,
        backend=backend,
        read_timeout=max(1.0, timeout),
        reconnect_initial=0.5,
        reconnect_max=1.0,
    )
    camera.start()
    return camera, camera.read_latest(timeout=timeout)


def _scan(max_index: int, backend: str, timeout: float) -> int:
    found: list[int] = []
    print(f"Scanning webcam indexes 0..{max_index} with backend={backend}")
    for index in range(max_index + 1):
        camera, packet = _open(index, backend, timeout)
        try:
            if packet is None:
                print(f"  index {index}: unavailable")
                continue
            height, width = packet.frame.shape[:2]
            found.append(index)
            print(f"  index {index}: AVAILABLE {width}x{height}")
        finally:
            camera.stop()
    if found:
        print("Available indexes: " + ", ".join(str(item) for item in found))
        print("Use the matching index for a device configured with source_type: webcam.")
        return 0
    print("No physical or virtual webcam source returned a frame.")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Find and preview physical/virtual Windows webcam sources."
    )
    parser.add_argument("--index", type=int, help="Open one webcam index")
    parser.add_argument(
        "--scan-max",
        type=int,
        default=4,
        help="Highest webcam index to scan when --index is omitted",
    )
    parser.add_argument(
        "--backend",
        choices=("dshow", "msmf", "auto"),
        default="dshow",
    )
    parser.add_argument("--timeout", type=float, default=3.0)
    preview_group = parser.add_mutually_exclusive_group()
    preview_group.add_argument(
        "--preview",
        action="store_true",
        help="Show one frozen full frame after capture stops (safe on the monitored PC)",
    )
    preview_group.add_argument(
        "--live-preview",
        action="store_true",
        help="Show live pixels; use only when the camera is not watching this display",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.ERROR, format="%(levelname)s: %(message)s")
    if args.index is None:
        if args.preview or args.live_preview:
            parser.error("--preview/--live-preview requires --index")
        return _scan(args.scan_max, args.backend, args.timeout)
    if args.index < 0 or args.timeout <= 0:
        parser.error("--index must be >= 0 and --timeout must be > 0")

    started = time.monotonic()
    camera, packet = _open(args.index, args.backend, args.timeout)
    try:
        if packet is None:
            print(f"FAILED: {camera.last_error or 'no frame received'}")
            return 1
        height, width = packet.frame.shape[:2]
        print(
            f"OK: webcam:{args.index} returned {width}x{height} in "
            f"{time.monotonic() - started:.2f}s"
        )
        if not args.preview and not args.live_preview:
            return 0

        if args.preview:
            camera.stop()
            print("Capture stopped. Showing one frozen frame; press Q to close.")
        else:
            print(
                "WARNING: live preview can recurse when the camera watches this display. "
                "Press Q to close."
            )
        sequence = packet.sequence
        while True:
            frame = packet.frame.copy()
            marker = max(20, min(frame.shape[1], frame.shape[0]) // 20)
            for x, y, sx, sy in (
                (0, 0, 1, 1),
                (frame.shape[1] - 1, 0, -1, 1),
                (frame.shape[1] - 1, frame.shape[0] - 1, -1, -1),
                (0, frame.shape[0] - 1, 1, -1),
            ):
                cv2.line(frame, (x, y), (x + sx * marker, y), (255, 255, 0), 4)
                cv2.line(frame, (x, y), (x, y + sy * marker), (255, 255, 0), 4)
            cv2.putText(
                frame,
                (
                    f"{'FROZEN' if args.preview else 'LIVE'} WEBCAM {args.index}  "
                    f"{frame.shape[1]}x{frame.shape[0]}"
                ),
                (20, 42),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.85,
                (255, 255, 255),
                2,
            )
            cv2.imshow("Camera Agent - webcam source check", resize_preview(frame))
            if cv2.waitKey(1) & 0xFF == ord("q"):
                break
            if args.live_preview:
                latest = camera.read_latest(after_sequence=sequence, timeout=0.1)
                if latest is not None:
                    packet = latest
                    sequence = latest.sequence
        cv2.destroyAllWindows()
        return 0
    finally:
        camera.stop()


if __name__ == "__main__":
    raise SystemExit(main())
