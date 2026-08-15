"""Idempotently create/verify Mainflux per-device MQTT control channels.

The controller Thing is deliberately supplied as an existing ID.  Creating a
Thing can generate a secret key, and this standalone tool has no transactional
secret sink; it therefore fails closed instead of printing such a key.  Use
the Mainflux UI/onboarding path to create the controller Thing and keep its
key in ``MAINFLUX_CONTROL_THING_KEY`` before running this tool.
"""

from __future__ import annotations

import argparse
from getpass import getpass
import os
from pathlib import Path
from urllib.parse import urlparse

import requests

from camera_agent.config import PROJECT_DIR, Settings
from camera_agent.fleet_config import FleetConfig
from camera_agent.provisioning import MainfluxProvisioner, ProvisioningError
from tools.setup_mainflux_rules import validate_login_input


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create/reconcile one private Mainflux MQTT control channel per device."
    )
    parser.add_argument("--group-id", required=True)
    parser.add_argument("--manifest", type=Path, default=PROJECT_DIR / "config" / "devices.yaml")
    parser.add_argument("--controller-thing-id", default=os.getenv("MAINFLUX_CONTROL_THING_ID"))
    parser.add_argument("--device-id", action="append", help="Repeat to limit selected manifest devices")
    parser.add_argument("--email", help="Mainflux login email; password is prompted and never stored")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def _channel_id_from_response(response: requests.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        if isinstance(payload.get("id"), str) and payload["id"].strip():
            return payload["id"].strip()
        channels = payload.get("channels")
        if isinstance(channels, list) and channels and isinstance(channels[0], dict):
            value = channels[0].get("id")
            if isinstance(value, str) and value.strip():
                return value.strip()
    location = response.headers.get("Location", "")
    path = urlparse(location).path.rstrip("/")
    value = path.rsplit("/", 1)[-1] if path else ""
    if value:
        return value
    raise ProvisioningError("Mainflux channel creation response did not include an ID")


def _things_from_response(response: requests.Response) -> set[str]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise ProvisioningError("Mainflux channel assignment response was invalid") from exc
    values = payload.get("things", payload.get("thing_ids", [])) if isinstance(payload, dict) else payload
    if not isinstance(values, list):
        raise ProvisioningError("Mainflux channel assignment response has no Thing list")
    result: set[str] = set()
    for value in values:
        if isinstance(value, dict):
            value = value.get("id")
        if value is not None and str(value).strip():
            result.add(str(value))
    return result


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    controller_id = (args.controller_thing_id or "").strip()
    if not controller_id:
        print("ERROR: --controller-thing-id or MAINFLUX_CONTROL_THING_ID is required")
        return 2
    settings = Settings.from_env()
    try:
        fleet = FleetConfig.from_yaml(args.manifest, settings, require_mainflux=False)
        selected = set(args.device_id or [item.device_id for item in fleet.devices])
        devices = [item for item in fleet.configured_devices if item.device_id in selected]
        if not devices or selected != {item.device_id for item in devices}:
            raise ProvisioningError("every --device-id must exist in the manifest")
        for device in devices:
            if not device.mainflux_thing_id:
                raise ProvisioningError(f"{device.device_id} needs mainflux.thing_id before control provisioning")
        email = (args.email or input("Mainflux email: ")).strip()
        password = getpass("Mainflux password (not stored): ")
        error = validate_login_input(email, password)
        if error:
            raise ProvisioningError(error)
        provisioner = MainfluxProvisioner(
            settings.mainflux_url, args.group_id, session=requests.Session(), verify_tls=settings.mainflux_verify_tls
        )
        provisioner.login(email, password)
        channels = provisioner._paginate("/channels", "channels")
        changes: list[tuple[str, str, str]] = []
        for device in devices:
            name = f"camera-agent-control-{device.device_id}"
            matches = [item for item in channels if item.get("name") == name]
            if len(matches) > 1:
                raise ProvisioningError(f"duplicate Mainflux control channels named {name}")
            if matches:
                channel_id = str(matches[0].get("id") or "")
                if not channel_id:
                    raise ProvisioningError(f"Mainflux control channel {name} has no ID")
            elif args.dry_run:
                changes.append((device.device_id, "would-create", "create channel and attach exactly controller + agent"))
                continue
            else:
                response = provisioner._send(
                    "POST", "/channels",
                    json={"name": name, "metadata": {"managed_by": "camera-agent-control", "device_id": device.device_id}},
                )
                channel_id = _channel_id_from_response(response)
                changes.append((device.device_id, channel_id, "created channel"))
            assigned = _things_from_response(provisioner._send("GET", f"/channels/{channel_id}/things"))
            required = {controller_id, str(device.mainflux_thing_id)}
            extra = assigned - required
            if extra:
                raise ProvisioningError(
                    f"control channel {channel_id} has foreign Thing assignment; no assignment was removed"
                )
            for thing_id in sorted(required - assigned):
                if not args.dry_run:
                    provisioner._send("PUT", f"/channels/{channel_id}/things/{thing_id}")
                changes.append((device.device_id, channel_id, f"attach Thing {thing_id}"))
            if device.mainflux_control_channel_id and device.mainflux_control_channel_id != channel_id:
                raise ProvisioningError(
                    f"{device.device_id} manifest control_channel_id differs from reconciled channel; no YAML was changed"
                )
            if not device.mainflux_control_channel_id:
                changes.append((device.device_id, channel_id, "set mainflux.control_channel_id to this ID in manifest"))
        for device_id, channel_id, action in changes:
            prefix = "WOULD" if args.dry_run else "OK"
            print(f"{prefix}: device={device_id} control_channel={channel_id} action={action}")
    except (ProvisioningError, ValueError, OSError) as exc:
        print(f"ERROR: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
