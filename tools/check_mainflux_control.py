"""Read-only MQTT/TLS preflight for the Camera Agent control plane.

It authenticates and subscribes to the already-provisioned per-device control
channel but never publishes a desired state, PTZ command, telemetry, image or
alarm event.  Run it on each agent host and on the central hub before rollout.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import time

from camera_agent.config import ConfigurationError, PROJECT_DIR, Settings
from camera_agent.fleet_config import FleetConfig
from camera_agent.mainflux_control import MainfluxControlError, MQTTClient, MQTTControlSettings


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read-only Mainflux MQTT control preflight.")
    parser.add_argument("--device-id", required=True)
    parser.add_argument("--manifest", type=Path, default=PROJECT_DIR / "config" / "devices.yaml")
    parser.add_argument("--env-file", type=Path, default=PROJECT_DIR / ".env")
    parser.add_argument("--role", choices=("agent", "controller"), default="agent")
    parser.add_argument("--timeout", type=float, default=8.0)
    return parser


def _device(args, settings: Settings):
    fleet = FleetConfig.from_yaml(args.manifest, settings, require_mainflux=False)
    matches = [item for item in fleet.configured_devices if item.device_id == args.device_id]
    if len(matches) != 1:
        raise ConfigurationError("device_id was not found exactly once in manifest")
    return matches[0]


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not 1.0 <= args.timeout <= 30.0:
        raise SystemExit("--timeout must be 1..30")
    try:
        settings = Settings.from_env(args.env_file)
        device = _device(args, settings)
        mqtt = MQTTControlSettings.from_environment()
        if mqtt is None:
            raise MainfluxControlError("MAINFLUX_MQTT_HOST is required")
        if not device.mainflux_control_channel_id:
            raise MainfluxControlError("device mainflux.control_channel_id is required")
        if args.role == "agent":
            thing_id = device.mainflux_thing_id
            thing_key = device.settings.mainflux_thing_key
            subscriptions = (
                mqtt.desired_topic(device.mainflux_control_channel_id, device.device_id),
                mqtt.command_topic(device.mainflux_control_channel_id, device.device_id),
            )
        else:
            thing_id = os.environ.get("MAINFLUX_CONTROL_THING_ID")
            thing_key = os.environ.get("MAINFLUX_CONTROL_THING_KEY")
            subscriptions = (mqtt.status_topic(device.mainflux_control_channel_id, device.device_id),)
        if not thing_id or not thing_key:
            raise MainfluxControlError("configured Thing ID and key are required for this role")
        client = MQTTClient(
            mqtt,
            client_id=thing_id,
            thing_key=thing_key,
        )
        client.start(subscriptions)
        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline and not client.connected:
            time.sleep(0.05)
        connected = client.connected
        client.stop()
        if not connected:
            print("ERROR: MQTT TLS/auth/subscription preflight did not connect")
            return 3
    except (ConfigurationError, MainfluxControlError, OSError) as exc:
        print(f"ERROR: {exc}")
        return 2
    print("OK: MQTT TLS/auth/subscription preflight passed; no control message was published")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
