from __future__ import annotations

import argparse
import os
from pathlib import Path
from urllib.parse import urljoin

import requests

from camera_agent.config import ConfigurationError, PROJECT_DIR, Settings
from camera_agent.fleet_config import FleetConfig


def senml_probe(
    *,
    rule_event: bool = False,
    camera_offline_event: bool = False,
) -> list[dict[str, object]]:
    """Return a valid SenML pack understood by Mainflux readers and rules."""
    return [
        {"n": "agent_state", "v": 0, "u": "code"},
        {"n": "camera_online", "v": int(not camera_offline_event), "u": "bool"},
        {"n": "computer_detected", "v": 0, "u": "bool"},
        {"n": "facebook_active", "v": 0, "u": "bool"},
        {"n": "rule_violation_event", "v": int(rule_event), "u": "bool"},
        {"n": "camera_offline_event", "v": int(camera_offline_event), "u": "bool"},
    ]


def _endpoint(base_url: str, path: str) -> str:
    return urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))


def resolve_device_publish_key(
    device_id: str,
    manifest_path: Path,
    base_settings: Settings,
) -> str:
    """Resolve exactly one device key without ever returning another device's key."""
    fleet = FleetConfig.from_yaml(
        manifest_path,
        base_settings,
        require_mainflux=False,
    )
    matches = [
        device
        for device in fleet.configured_devices
        if device.device_id == device_id
    ]
    if len(matches) != 1:
        raise ConfigurationError(
            f"Device {device_id!r} was not found exactly once in {manifest_path}"
        )
    device = matches[0]
    key = device.settings.mainflux_thing_key
    if not key:
        reference = device.mainflux_thing_key_env or "<missing thing_key_env>"
        raise ConfigurationError(
            f"Environment variable {reference} for device {device_id!r} is empty"
        )
    return key


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Check Mainflux health and optionally publish a SenML probe."
    )
    parser.add_argument(
        "--device-id",
        help="Required for publishing; selects the device-specific Thing key.",
    )
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--env-file", type=Path, default=PROJECT_DIR / ".env")
    parser.add_argument("--publish-test", action="store_true")
    parser.add_argument(
        "--publish-rule-event",
        action="store_true",
        help="Publish rule_violation_event=1; this intentionally creates an alarm.",
    )
    parser.add_argument(
        "--publish-camera-offline-event",
        action="store_true",
        help="Publish camera_offline_event=1; this intentionally creates an alarm.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = Settings.from_env(args.env_file)
    base_url = settings.mainflux_url
    messages_path = settings.mainflux_messages_path
    verify_tls = settings.mainflux_verify_tls

    try:
        health = requests.get(
            _endpoint(base_url, "/health"), timeout=5, verify=verify_tls
        )
        health.raise_for_status()
    except requests.RequestException as exc:
        print(f"ERROR: Mainflux health check failed: {exc}")
        return 2

    body = health.json()
    print(
        "OK: Mainflux is healthy"
        f" (version={body.get('version', 'unknown')}, status={body.get('status', 'unknown')})"
    )

    should_publish = (
        args.publish_test
        or args.publish_rule_event
        or args.publish_camera_offline_event
    )
    if not should_publish:
        print("Read-only check only. Add --publish-test to verify Thing publishing.")
        return 0

    if not args.device_id:
        print(
            "ERROR: pass --device-id when publishing so the probe cannot use the "
            "wrong device Thing key"
        )
        return 2
    manifest_path = args.manifest or Path(
        os.getenv("DEVICES_FILE", str(PROJECT_DIR / "config" / "devices.yaml"))
    )
    try:
        key = resolve_device_publish_key(args.device_id, manifest_path, settings)
    except (ConfigurationError, FileNotFoundError) as exc:
        print(f"ERROR: {exc}")
        return 2
    scheme = settings.mainflux_auth_scheme
    authorization = " ".join(part for part in (scheme, key) if part)
    try:
        response = requests.post(
            _endpoint(base_url, messages_path),
            headers={
                "Authorization": authorization,
                "Content-Type": "application/senml+json",
            },
            json=senml_probe(
                rule_event=args.publish_rule_event,
                camera_offline_event=args.publish_camera_offline_event,
            ),
            timeout=5,
            verify=verify_tls,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        print(f"ERROR: Mainflux rejected the SenML probe: {exc}")
        return 3

    event_names = []
    if args.publish_rule_event:
        event_names.append("Facebook rule")
    if args.publish_camera_offline_event:
        event_names.append("camera offline")
    event_note = f" with {' and '.join(event_names)} event" if event_names else ""
    print(f"OK: Mainflux accepted the SenML probe{event_note} (HTTP {response.status_code})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
