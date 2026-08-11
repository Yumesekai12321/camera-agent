from __future__ import annotations

import argparse
from pathlib import Path
import time

import cv2

from camera_agent.application import resize_preview
from camera_agent.camera import RTSPCamera
from camera_agent.config import PROJECT_DIR, Settings
from camera_agent.vision import ComputerScreenDetector


CLASSES = {
    ord("a"): "facebook_active",
    ord("m"): "facebook_mention",
    ord("o"): "other",
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect labeled camera monitor crops.")
    parser.add_argument(
        "--output",
        type=Path,
        default=PROJECT_DIR / "data" / "facebook" / "cropped",
        help="Use a separate device/session folder for independent evaluation.",
    )
    args = parser.parse_args()
    settings = Settings.from_env()
    settings.validate(require_mainflux=False)
    output_root = args.output
    for class_name in CLASSES.values():
        (output_root / class_name).mkdir(parents=True, exist_ok=True)

    detector = ComputerScreenDetector(
        settings.computer_model,
        confidence=settings.computer_confidence,
        image_size=settings.detector_image_size,
        normalized_roi=settings.monitor_roi,
    )
    camera = RTSPCamera(
        settings.rtsp_url,
        safe_url=settings.redacted_rtsp_url,
        open_timeout=settings.camera_open_timeout,
        read_timeout=settings.camera_read_timeout,
        reconnect_initial=settings.reconnect_initial,
        reconnect_max=settings.reconnect_max,
    )
    label: str | None = None
    burst = False
    last_saved = 0.0
    last_sequence = -1
    counts = {name: 0 for name in CLASSES.values()}

    print(
        "A=facebook_active  M=facebook_mention  O=other  "
        "SPACE=save  B=burst  Q=quit"
    )
    with camera:
        while True:
            packet = camera.read_latest(after_sequence=last_sequence, timeout=1)
            if packet is None:
                print(f"Waiting for camera: {camera.last_error or 'no frame'}")
                continue
            last_sequence = packet.sequence
            extraction = detector.extract(packet.frame)
            screen = extraction.screen
            display = resize_preview(screen if screen is not None else packet.frame, 1000, 700)
            status = f"LABEL={label or 'NONE'} BURST={'ON' if burst else 'OFF'}"
            cv2.putText(
                display,
                status,
                (20, 35),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.7,
                (0, 255, 255),
                2,
            )
            cv2.imshow("Camera Agent - Facebook dataset collector", display)

            now = time.monotonic()
            key = cv2.waitKey(1) & 0xFF
            if key in CLASSES:
                label = CLASSES[key]
                print(f"Label: {label}")
            elif key == ord("b"):
                burst = not burst
                last_saved = 0.0
            elif key == ord("q"):
                break

            should_save = key == ord(" ") or (
                burst and label is not None and now - last_saved >= 2.0
            )
            if should_save:
                if label is None:
                    print("Choose A, M or O first")
                elif screen is None:
                    print("No monitor crop; frame was not saved")
                else:
                    target = output_root / label / f"{label}_{time.time_ns()}.jpg"
                    if cv2.imwrite(str(target), screen):
                        counts[label] += 1
                        last_saved = now
                        print(f"Saved {target.name}")
    cv2.destroyAllWindows()
    print("Saved:", counts)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
