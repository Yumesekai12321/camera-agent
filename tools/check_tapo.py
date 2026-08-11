from __future__ import annotations

import argparse
import time

import cv2

from camera_agent.application import resize_preview
from camera_agent.camera import RTSPCamera
from camera_agent.config import ConfigurationError, Settings


def main() -> int:
    parser = argparse.ArgumentParser(description="Open Tapo RTSP and read a fresh frame.")
    parser.add_argument("--timeout", type=float, default=15.0)
    preview_group = parser.add_mutually_exclusive_group()
    preview_group.add_argument(
        "--preview",
        action="store_true",
        help="Show one frozen full frame after capture stops; Q quits.",
    )
    preview_group.add_argument(
        "--live-preview",
        action="store_true",
        help="Show live pixels; use only when the camera is not watching this display.",
    )
    args = parser.parse_args()

    settings = Settings.from_env()
    try:
        settings.validate(require_models=False, require_mainflux=False)
    except ConfigurationError as exc:
        print(exc)
        return 2

    print(f"RTSP: {settings.redacted_rtsp_url}")
    camera = RTSPCamera(
        settings.rtsp_url,
        safe_url=settings.redacted_rtsp_url,
        open_timeout=settings.camera_open_timeout,
        read_timeout=settings.camera_read_timeout,
        reconnect_initial=settings.reconnect_initial,
        reconnect_max=settings.reconnect_max,
    )
    started = time.monotonic()
    with camera:
        packet = camera.read_latest(timeout=args.timeout)
        if packet is None:
            print(f"FAILED: {camera.last_error or 'no frame received'}")
            return 1
        height, width = packet.frame.shape[:2]
        print(f"OK: received {width}x{height} frame in {time.monotonic() - started:.2f}s")
        if args.preview or args.live_preview:
            if args.preview:
                camera.stop()
                print(
                    "Capture stopped. Cyan L marks show the four real frame corners. "
                    "Press Q to quit."
                )
            else:
                print(
                    "WARNING: live preview can recurse when the camera watches this "
                    "display. Press Q to quit."
                )
            sequence = packet.sequence
            while True:
                frame = packet.frame.copy()
                frame_height, frame_width = frame.shape[:2]
                marker = max(20, min(frame_width, frame_height) // 20)
                for x, y, sx, sy in (
                    (0, 0, 1, 1),
                    (frame_width - 1, 0, -1, 1),
                    (frame_width - 1, frame_height - 1, -1, -1),
                    (0, frame_height - 1, 1, -1),
                ):
                    cv2.line(frame, (x, y), (x + sx * marker, y), (255, 255, 0), 4)
                    cv2.line(frame, (x, y), (x, y + sy * marker), (255, 255, 0), 4)
                cv2.putText(
                    frame,
                    (
                        f"{'FROZEN' if args.preview else 'LIVE'} FULL RTSP FRAME "
                        f"{frame_width}x{frame_height}"
                    ),
                    (20, 45),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.9,
                    (255, 255, 255),
                    2,
                )
                cv2.imshow("Tapo FULL RTSP frame", resize_preview(frame))
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
