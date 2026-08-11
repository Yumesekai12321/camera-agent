from __future__ import annotations

import argparse
from dataclasses import replace
import logging
import sys
import time
from pathlib import Path

from camera_agent.config import ConfigurationError, Settings
from camera_agent.fleet_config import FleetConfig


LOGGER = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Trigger a Tapo physical siren for one manifest device. "
            "This intentionally makes the camera audible."
        )
    )
    parser.add_argument("--devices", type=Path, default=Path("config/devices.yaml"))
    parser.add_argument("--device-id", required=True)
    parser.add_argument("--duration", type=float, default=3.0)
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Actually trigger the physical siren. Without this flag only validate.",
    )
    return parser


def _load_device(devices_path: Path, device_id: str):
    settings = Settings.from_env()
    settings.validate(require_camera=False, require_models=False, require_mainflux=False)
    fleet = FleetConfig.from_yaml(devices_path, settings, require_mainflux=False)
    for device in fleet.configured_devices:
        if device.device_id == device_id:
            return device
    raise ConfigurationError(f"Device not found in manifest: {device_id}")


def _validate_tapo_alarm_device(device) -> None:
    settings = device.settings
    if device.source_type != "rtsp" or settings.rtsp_adapter != "tapo":
        raise ConfigurationError("Physical C200 alarm requires source_type=rtsp adapter=tapo")
    if settings.rtsp_url_override:
        raise ConfigurationError("Physical C200 alarm requires structured host/env credentials")
    if not (settings.rtsp_host and settings.rtsp_username and settings.rtsp_password):
        raise ConfigurationError("Physical C200 alarm requires host, username_env and password_env")


def trigger_tapo_siren(device, *, duration: float) -> None:
    try:
        from pytapo import Tapo
    except ImportError as exc:
        raise ConfigurationError(
            "Missing optional package 'pytapo'. Install with: "
            ".\\.venv\\Scripts\\python.exe -m pip install pytapo"
        ) from exc

    camera = Tapo(
        device.settings.rtsp_host,
        device.settings.rtsp_username,
        device.settings.rtsp_password,
        printDebugInformation=False,
        printWarnInformation=False,
    )
    try:
        camera.setSirenStatus(True)
        time.sleep(duration)
    finally:
        try:
            camera.setSirenStatus(False)
        except Exception:
            pass


def main() -> int:
    args = build_parser().parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    try:
        if args.duration <= 0:
            raise ConfigurationError("--duration must be greater than zero")
        device = _load_device(args.devices, args.device_id)
        _validate_tapo_alarm_device(device)
        if not args.yes:
            print(
                f"OK: {args.device_id} can be addressed as a Tapo alarm target "
                f"at {device.settings.rtsp_host}. Re-run with --yes to sound it."
            )
            return 0
        trigger_tapo_siren(replace(device, physical_alarm_enabled=True), duration=args.duration)
        print(f"OK: Triggered Tapo physical siren for {args.device_id}")
        return 0
    except ConfigurationError as exc:
        LOGGER.error("%s", exc)
        return 2
    except Exception as exc:
        LOGGER.error("Tapo physical siren failed: %s", exc)
        return 3


if __name__ == "__main__":
    sys.exit(main())
