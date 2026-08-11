from __future__ import annotations

import argparse
import time

import cv2

from camera_agent.application import resize_preview
from camera_agent.camera import WindowCamera


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check a named Windows window as a live camera source."
    )
    parser.add_argument(
        "--title",
        required=True,
        help="Exact title of the window to capture, for example CAMERA-LIVE",
    )
    parser.add_argument(
        "--method",
        choices=("auto", "printwindow", "screen"),
        default="auto",
    )
    parser.add_argument("--fps", type=float, default=8.0)
    parser.add_argument(
        "--minimum-frame-std",
        type=float,
        default=2.0,
        help="Treat nearly uniform/black frames as capture failures; use 0 to disable",
    )
    parser.add_argument("--timeout", type=float, default=12.0)
    preview_group = parser.add_mutually_exclusive_group()
    preview_group.add_argument(
        "--preview",
        action="store_true",
        help="Show one frozen frame after capture stops (safe for screen method)",
    )
    preview_group.add_argument(
        "--live-preview",
        action="store_true",
        help="Show live capture; screen method may recurse if windows overlap",
    )
    args = parser.parse_args()

    camera = WindowCamera(
        args.title,
        capture_method=args.method,
        frames_per_second=args.fps,
        minimum_frame_std=args.minimum_frame_std,
        read_timeout=args.timeout,
        reconnect_initial=0.5,
        reconnect_max=2.0,
    )
    started = time.monotonic()
    with camera:
        packet = camera.read_latest(timeout=args.timeout)
        if packet is None:
            print(f"FAILED: {camera.last_error or 'no frame received'}")
            print(
                f"Start the source window with title {args.title!r}. If auto returns "
                "black video, retry with --method screen and keep the window visible."
            )
            return 1

        height, width = packet.frame.shape[:2]
        print(
            f"OK: window:{args.title} returned {width}x{height} in "
            f"{time.monotonic() - started:.2f}s using {camera.active_capture_method}; "
            f"aspect={width / height:.3f}"
        )
        if float(packet.frame.std()) < 2.0:
            print(
                "WARNING: frame is almost uniform/black. Retry with --method screen "
                "and leave the source window restored and unobstructed."
            )
        if not args.preview and not args.live_preview:
            return 0

        if args.preview:
            camera.stop()
            print(
                "Capture stopped. Showing one frozen full client frame. If all four "
                "cyan L marks are visible, WindowCamera captured the complete source "
                "client area. Press Q."
            )
        else:
            print(
                "WARNING: live screen capture recurses if this preview covers the source window. "
                "Press Q to close."
            )
        sequence = packet.sequence
        while True:
            frame = packet.frame.copy()
            marker = max(16, min(frame.shape[1], frame.shape[0]) // 16)
            for x, y, sx, sy in (
                (0, 0, 1, 1),
                (frame.shape[1] - 1, 0, -1, 1),
                (frame.shape[1] - 1, frame.shape[0] - 1, -1, -1),
                (0, frame.shape[0] - 1, 1, -1),
            ):
                cv2.line(frame, (x, y), (x + sx * marker, y), (255, 255, 0), 3)
                cv2.line(frame, (x, y), (x, y + sy * marker), (255, 255, 0), 3)
            cv2.putText(
                frame,
                (
                    f"{'FROZEN' if args.preview else 'LIVE'} WINDOW {args.title}  "
                    f"{frame.shape[1]}x{frame.shape[0]}"
                ),
                (20, 42),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.75,
                (255, 255, 255),
                2,
            )
            cv2.imshow("Camera Agent - window source check", resize_preview(frame))
            if cv2.waitKey(1) & 0xFF == ord("q"):
                break
            if args.live_preview:
                latest = camera.read_latest(after_sequence=sequence, timeout=0.1)
                if latest is not None:
                    packet = latest
                    sequence = latest.sequence
        cv2.destroyAllWindows()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
