from __future__ import annotations

import argparse
import os
from pathlib import Path

import cv2

from camera_agent.application import resize_preview
from camera_agent.camera import RTSPCamera, build_device_camera
from camera_agent.config import Settings
from camera_agent.fleet_config import FleetConfig


def main() -> int:
    parser = argparse.ArgumentParser(description="Select a fixed monitor ROI.")
    parser.add_argument(
        "--devices",
        type=Path,
        help="Fleet manifest; defaults to DEVICES_FILE when --device is used",
    )
    parser.add_argument("--device", help="Fleet device id, for example camera-01")
    args = parser.parse_args()

    settings = Settings.from_env()
    selected_device = None
    if args.device:
        devices_path = args.devices
        if devices_path is None:
            devices_path = Path(os.getenv("DEVICES_FILE", "config/devices.yaml"))
        fleet = FleetConfig.from_yaml(
            devices_path,
            settings,
            require_mainflux=False,
        )
        selected_device = next(
            (item for item in fleet.devices if item.device_id == args.device),
            None,
        )
        if selected_device is None:
            print(
                f"Device {args.device!r} is not enabled in {devices_path}; "
                "set enabled: true temporarily"
            )
            return 2
        camera = build_device_camera(selected_device)
        print(f"Waiting for {selected_device.name} ({camera.safe_url})...")
    else:
        settings.validate(require_models=False, require_mainflux=False)
        camera = RTSPCamera(
            settings.rtsp_url,
            safe_url=settings.redacted_rtsp_url,
            open_timeout=settings.camera_open_timeout,
            read_timeout=settings.camera_read_timeout,
            reconnect_initial=settings.reconnect_initial,
            reconnect_max=settings.reconnect_max,
        )
        print("Waiting for a camera frame...")
    with camera:
        packet = camera.read_latest(timeout=15)
    if packet is None:
        print(f"FAILED: {camera.last_error or 'no frame received'}")
        return 1

    frame = packet.frame
    preview = resize_preview(frame)
    scale_x = frame.shape[1] / preview.shape[1]
    scale_y = frame.shape[0] / preview.shape[0]
    print("Drag a box around one monitor, then press ENTER or SPACE. ESC cancels.")
    x, y, width, height = cv2.selectROI(
        "Select monitor ROI",
        preview,
        fromCenter=False,
        showCrosshair=True,
    )
    cv2.destroyAllWindows()
    if width == 0 or height == 0:
        print("Cancelled")
        return 1

    x *= scale_x
    width *= scale_x
    y *= scale_y
    height *= scale_y
    frame_width, frame_height = frame.shape[1], frame.shape[0]
    normalized = (
        x / frame_width,
        y / frame_height,
        width / frame_width,
        height / frame_height,
    )
    value = ",".join(f"{item:.6f}" for item in normalized)
    if selected_device is None:
        print("\nAdd or replace this line in .env:")
        print(f"MONITOR_ROI={value}")
    else:
        print(f"\nSet this field on device {selected_device.device_id} in the manifest:")
        print(f"monitor_roi: [{', '.join(f'{item:.6f}' for item in normalized)}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
