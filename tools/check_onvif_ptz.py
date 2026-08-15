"""Validate an explicitly enabled ONVIF PTZ device before running patrol."""

from __future__ import annotations

import argparse
from pathlib import Path

from camera_agent.config import PROJECT_DIR, Settings
from camera_agent.fleet_config import FleetConfig
from camera_agent.ptz import OnvifPTZ, PTZError, PTZMove


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Probe ONVIF PTZ without moving by default.")
    parser.add_argument("--devices", type=Path, default=PROJECT_DIR / "config" / "devices.yaml")
    parser.add_argument("--device-id", required=True)
    parser.add_argument("--move", choices=[item.value for item in PTZMove])
    parser.add_argument("--duration", type=float)
    parser.add_argument("--yes", action="store_true", help="Required with --move because it moves the physical camera.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.move and not args.yes:
        raise SystemExit("--move changes physical camera position; repeat with --yes after reviewing direction.")
    if args.duration is not None and not args.move:
        raise SystemExit("--duration requires --move")
    settings = Settings.from_env()
    fleet = FleetConfig.from_yaml(args.devices, settings, require_mainflux=False)
    device = next((item for item in fleet.configured_devices if item.device_id == args.device_id), None)
    if device is None:
        raise SystemExit(f"Unknown device_id: {args.device_id}")
    if not device.ptz_enabled:
        raise SystemExit(f"Device {args.device_id} has ptz.enabled: false")
    ptz = OnvifPTZ(
        host=device.settings.rtsp_host or "",
        port=device.ptz_port,
        username=device.settings.rtsp_username or "",
        password=device.settings.rtsp_password or "",
        profile_token=device.ptz_profile_token,
        velocity=device.ptz_velocity,
        move_duration_seconds=device.ptz_move_duration_seconds,
    )
    try:
        ptz.probe()
        print(f"OK: ONVIF PTZ profile discovered for {args.device_id}; camera was not moved.")
        if args.move:
            ptz.move(PTZMove(args.move), duration_seconds=args.duration)
            print(f"OK: moved {args.move} once and sent ONVIF Stop.")
    except PTZError as exc:
        print(f"ERROR: ONVIF PTZ check failed: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
