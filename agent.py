from __future__ import annotations

import argparse
from collections.abc import Mapping
from dataclasses import replace
import logging
import os
from pathlib import Path
import sys

from camera_agent.application import CameraAgent
from camera_agent.config import ConfigurationError, PROJECT_DIR, Settings
from camera_agent.fleet import FleetAgent
from camera_agent.fleet_config import FleetConfig
from camera_agent.rules import RuleConfigurationError, RulesConfig


def resolve_devices_path(
    explicit: Path | None,
    environ: Mapping[str, str],
    *,
    project_dir: Path = PROJECT_DIR,
) -> Path | None:
    """Select the v2 fleet manifest while retaining the legacy single-source fallback."""
    if explicit is not None:
        return explicit
    configured = environ.get("DEVICES_FILE", "").strip()
    if configured:
        return Path(configured)
    canonical = project_dir / "config" / "devices.yaml"
    return canonical if canonical.is_file() else None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Classify computer-screen activity from RTSP, ADB, window or webcam sources."
        )
    )
    parser.add_argument(
        "--devices",
        type=Path,
        help="Run all enabled devices/sources from a version 2 YAML manifest.",
    )
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="Validate configuration and model files without opening the camera.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run inference but do not publish telemetry to Mainflux.",
    )
    parser.add_argument(
        "--no-preview",
        action="store_true",
        help="Disable OpenCV windows for unattended/headless operation.",
    )
    parser.add_argument(
        "--preview-mode",
        choices=("full", "compact", "off"),
        help="Override PREVIEW_MODE for this run.",
    )
    parser.add_argument(
        "--max-cycles",
        type=int,
        default=None,
        help=argparse.SUPPRESS,
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    try:
        settings = Settings.from_env()
        devices_path = resolve_devices_path(args.devices, os.environ)

        fleet: FleetConfig | None = None
        if devices_path is not None:
            settings.validate(require_camera=False, require_mainflux=False)
            fleet = FleetConfig.from_yaml(
                devices_path,
                settings,
                require_mainflux=not args.dry_run,
            )
        else:
            settings.validate(
                require_camera=not args.check_config,
                require_mainflux=not args.dry_run,
            )
        RulesConfig.from_toml(settings.rules_file)
        if args.check_config:
            print("Configuration and model files: OK")
            print(f"Computer model : {settings.computer_model}")
            print(f"Facebook model : {settings.classifier_model}")
            print(f"Mainflux       : {'enabled' if settings.mainflux_enabled else 'disabled'}")
            print(f"Preview mode   : {settings.preview_mode if settings.preview else 'off'}")
            print(f"Rules          : {settings.rules_file}")
            if fleet is not None:
                print(f"Fleet manifest : {devices_path}")
                print(
                    "Fleet devices  : "
                    f"{len(fleet.configured_devices)} configured, {len(fleet.devices)} enabled"
                )
                for device in fleet.configured_devices:
                    source = device.source_type
                    if device.webcam_index is not None:
                        source = f"webcam:{device.webcam_index}"
                    elif device.window_title:
                        source = f"window:{device.window_title}"
                    alarm = (
                        f", physical_alarm:{device.physical_alarm_provider}"
                        if device.physical_alarm_enabled
                        else ""
                    )
                    state = "enabled" if device.enabled else "disabled"
                    print(
                        f"  - {device.device_id}: {device.name} "
                        f"[{source}, {state}{alarm}]"
                    )
            return 0
        if fleet is not None:
            if args.preview_mode == "full":
                raise ConfigurationError(
                    "Fleet mode supports compact or off preview only; camera pixels "
                    "are intentionally hidden to prevent mirror reflection"
                )
            if args.no_preview or args.preview_mode == "off":
                fleet = replace(fleet, preview=False)
            elif args.preview_mode == "compact":
                fleet = replace(fleet, preview=True)
            FleetAgent(fleet, dry_run=args.dry_run).run(max_cycles=args.max_cycles)
            return 0
        if args.no_preview:
            settings = replace(settings, preview=False, preview_mode="off")
        elif args.preview_mode:
            settings = replace(
                settings,
                preview=args.preview_mode != "off",
                preview_mode=args.preview_mode,
            )
        CameraAgent(settings, dry_run=args.dry_run).run(max_cycles=args.max_cycles)
        return 0
    except (ConfigurationError, RuleConfigurationError, FileNotFoundError) as exc:
        logging.error("%s", exc)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
