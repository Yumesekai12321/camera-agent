from __future__ import annotations

import argparse
import shutil
import subprocess
import time

import cv2

from camera_agent.application import resize_preview
from camera_agent.camera import ADBCamera


def parse_adb_devices(output: str) -> dict[str, str]:
    """Parse `adb devices -l` without retaining device metadata."""
    devices: dict[str, str] = {}
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("*") or line.startswith("List of devices"):
            continue
        fields = line.split()
        if len(fields) >= 2:
            devices[fields[0]] = fields[1]
    return devices


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Preflight the legacy HVIP01 adapter through Android ADB; scrcpy is optional."
        )
    )
    parser.add_argument("--title", default="HVIP01-LIVE")
    parser.add_argument("--timeout", type=float, default=8.0)
    parser.add_argument("--serial", help="ADB serial; required only with multiple devices")
    parser.add_argument(
        "--preview",
        action="store_true",
        help="Show one frozen Android frame after capture stops",
    )
    args = parser.parse_args()

    adb = shutil.which("adb")
    scrcpy = shutil.which("scrcpy")
    if adb is None:
        print("FAILED: adb is not available in PATH (install Android Platform Tools).")
    else:
        print(f"OK: adb found at {adb}")
    if scrcpy is None:
        print("INFO: scrcpy is not available; it is optional for observation.")
    else:
        print(f"OK: scrcpy found at {scrcpy}")
    if adb is None:
        return 2

    try:
        result = subprocess.run(
            [adb, "devices", "-l"],
            check=False,
            capture_output=True,
            text=True,
            timeout=args.timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"FAILED: could not query ADB devices: {exc}")
        return 2
    if result.returncode != 0:
        message = (result.stderr or result.stdout).strip() or "unknown ADB error"
        print(f"FAILED: adb devices returned an error: {message}")
        return 2

    devices = parse_adb_devices(result.stdout)
    ready = [serial for serial, status in devices.items() if status == "device"]
    if not ready:
        if any(status == "unauthorized" for status in devices.values()):
            print("FAILED: Android is unauthorized; accept the USB debugging prompt.")
        elif devices:
            statuses = ", ".join(sorted(set(devices.values())))
            print(f"FAILED: no ready Android device (status: {statuses}).")
        else:
            print("FAILED: no Android device detected; connect USB and enable USB debugging.")
        return 2
    print(f"OK: {len(ready)} authorized Android device(s) available")
    if args.serial:
        if args.serial not in ready:
            print("FAILED: requested Android serial is not authorized/connected.")
            return 2
        serial = args.serial
    elif len(ready) == 1:
        serial = ready[0]
    else:
        print("FAILED: multiple Android devices found; rerun with --serial SERIAL.")
        return 2

    started = time.monotonic()
    camera = ADBCamera(
        serial,
        frames_per_second=1.0,
        command_timeout=args.timeout,
        require_landscape=True,
        minimum_frame_std=2.0,
    )
    with camera:
        packet = camera.read_latest(timeout=args.timeout + 1)
        if packet is None:
            print(f"FAILED: {camera.last_error or 'no Android frame received'}")
            return 1
        height, width = packet.frame.shape[:2]
        print(
            f"OK: full Android display returned {width}x{height} in "
            f"{time.monotonic() - started:.2f}s"
        )
        if args.preview:
            camera.stop()
            frame = packet.frame.copy()
            marker = max(20, min(width, height) // 20)
            for x, y, sx, sy in (
                (0, 0, 1, 1),
                (width - 1, 0, -1, 1),
                (width - 1, height - 1, -1, -1),
                (0, height - 1, 1, -1),
            ):
                cv2.line(frame, (x, y), (x + sx * marker, y), (255, 255, 0), 4)
                cv2.line(frame, (x, y), (x, y + sy * marker), (255, 255, 0), 4)
            cv2.imshow("HVIP01 - frozen Android display", resize_preview(frame))
            print("Capture stopped. Showing one frozen frame; press Q.")
            while cv2.waitKey(20) & 0xFF != ord("q"):
                pass
            cv2.destroyAllWindows()
    if scrcpy is not None:
        print("OPTIONAL observation window (agent does not capture it):")
        print(
            f"scrcpy --no-audio --max-size=1280 --max-fps=15 "
            f"--window-title={args.title}"
        )
    print("NEXT: calibrate this adapter ROI or run its manifest entry in --dry-run mode.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
