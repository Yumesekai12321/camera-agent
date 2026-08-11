from __future__ import annotations

import argparse
from collections.abc import Sequence
from getpass import getpass
import os
import re

from dotenv import load_dotenv
import requests

from camera_agent.provisioning import (
    MainfluxProvisioner,
    PROFILE_NAME,
    ProvisioningError,
    build_profile,
    build_profile_create_payload,
    build_rules,
    profile_is_current,
)


EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
DEVICE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")


def validate_login_input(email: str, password: str) -> str | None:
    if not EMAIL_PATTERN.fullmatch(email):
        return "email must be the full Mainflux login email, for example name@example.com"
    if len(password) < 8 or any(character.isspace() for character in password):
        return "Mainflux password must contain at least 8 non-whitespace characters"
    return None


def normalize_thing_ids(thing_ids: str | Sequence[str]) -> list[str]:
    values = [thing_ids] if isinstance(thing_ids, str) else list(thing_ids)
    normalized = list(dict.fromkeys(value.strip() for value in values if value.strip()))
    if not normalized:
        raise ValueError("At least one Mainflux Thing UUID is required")
    return normalized


def _normalize_device_id(value: str) -> str:
    device_id = value.strip().lower()
    if not DEVICE_ID_PATTERN.fullmatch(device_id):
        raise ValueError(
            "device_id must use lowercase letters, numbers, underscores or hyphens"
        )
    return device_id


def parse_device_targets(
    device_specs: Sequence[str],
    device_ids: Sequence[str],
    thing_ids: Sequence[str],
) -> list[tuple[str, str | None]]:
    """Parse non-secret device-to-Thing mappings accepted by the CLI."""

    targets: list[tuple[str, str | None]] = []
    for spec in device_specs:
        raw_device, separator, raw_thing = spec.partition("=")
        device_id = _normalize_device_id(raw_device)
        thing_id = raw_thing.strip() if separator and raw_thing.strip() else None
        targets.append((device_id, thing_id))

    if thing_ids and not device_ids:
        raise ValueError("--thing-id requires a matching --device-id")
    if thing_ids and len(thing_ids) != len(device_ids):
        raise ValueError("repeat --thing-id exactly once for every --device-id")
    for index, raw_device in enumerate(device_ids):
        device_id = _normalize_device_id(raw_device)
        thing_id = thing_ids[index].strip() if thing_ids else None
        if thing_ids and not thing_id:
            raise ValueError("Mainflux Thing ID cannot be empty")
        targets.append((device_id, thing_id))

    if not targets:
        raise ValueError("provide at least one --device or --device-id")
    seen: set[str] = set()
    seen_thing_ids: set[str] = set()
    for device_id, _ in targets:
        if device_id in seen:
            raise ValueError(f"Duplicate device_id: {device_id}")
        seen.add(device_id)
    for _, thing_id in targets:
        if thing_id is None:
            continue
        normalized_thing_id = thing_id.casefold()
        if normalized_thing_id in seen_thing_ids:
            raise ValueError(f"Duplicate Mainflux Thing ID: {thing_id}")
        seen_thing_ids.add(normalized_thing_id)
    return targets


def _parse_display_names(values: Sequence[str]) -> dict[str, str]:
    names: dict[str, str] = {}
    for value in values:
        raw_device, separator, raw_name = value.partition("=")
        if not separator or not raw_name.strip():
            raise ValueError("--display-name must use DEVICE_ID=DISPLAY_NAME")
        device_id = _normalize_device_id(raw_device)
        if device_id in names:
            raise ValueError(f"Duplicate display name for device_id: {device_id}")
        names[device_id] = raw_name.strip()
    return names


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Reconcile an existing Mainflux Thing and two isolated Camera Agent "
            "rules per device on Mainflux v0.41.1. This standalone command does "
            "not create Things because it has no safe local sink for generated keys."
        )
    )
    parser.add_argument("--group-id", required=True, help="Target Mainflux group UUID")
    parser.add_argument(
        "--device",
        action="append",
        default=[],
        metavar="DEVICE_ID[=THING_ID]",
        help=(
            "Device to reconcile; omit THING_ID to find its existing deterministic "
            "Thing. To create one safely, use tools.add_device --provision-mainflux. "
            "Repeat for multiple devices."
        ),
    )
    parser.add_argument(
        "--device-id",
        action="append",
        default=[],
        help="Device ID whose existing deterministic Thing should be found",
    )
    parser.add_argument(
        "--thing-id",
        action="append",
        default=[],
        help="Existing Thing UUID paired positionally with each --device-id",
    )
    parser.add_argument(
        "--display-name",
        action="append",
        default=[],
        metavar="DEVICE_ID=DISPLAY_NAME",
        help="Optional human-readable Thing display name metadata",
    )
    parser.add_argument("--email", help="Mainflux login email; password is always prompted")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Read current state and show the plan without changing Mainflux",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        targets = parse_device_targets(args.device, args.device_id, args.thing_id)
        display_names = _parse_display_names(args.display_name)
    except ValueError as exc:
        parser.error(str(exc))

    unknown_names = set(display_names) - {device_id for device_id, _ in targets}
    if unknown_names:
        parser.error(
            "--display-name references an unselected device_id: "
            + ", ".join(sorted(unknown_names))
        )

    load_dotenv()
    base_url = os.getenv("MAINFLUX_URL", "http://localhost").strip().rstrip("/")
    verify_tls = os.getenv("MAINFLUX_VERIFY_TLS", "true").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }
    email = (args.email or input("Mainflux email: ")).strip()
    password = getpass("Mainflux password (not stored): ")
    input_error = validate_login_input(email, password)
    if input_error:
        print(f"ERROR: {input_error}")
        print(
            "Use the account that logs in at MAINFLUX_URL, not a camera account."
        )
        return 2

    provisioner = MainfluxProvisioner(
        base_url=base_url,
        group_id=args.group_id,
        session=requests.Session(),
        verify_tls=verify_tls,
    )
    try:
        provisioner.login(email, password)
        results = []
        for device_id, thing_id in targets:
            results.append(
                provisioner.provision_device(
                    device_id,
                    display_names.get(device_id, device_id),
                    thing_id=thing_id,
                    dry_run=args.dry_run,
                    allow_create=False,
                )
            )
    except (ProvisioningError, ValueError) as exc:
        print(f"ERROR: {exc}")
        return 2

    for result in results:
        thing_label = result.thing_id or "would-create"
        profile_label = result.profile_id or "would-create"
        rule_labels = ", ".join(
            f"{kind}={rule_id}" for kind, rule_id in sorted(result.rule_ids.items())
        ) or "would-create"
        prefix = "WOULD CONFIGURE" if result.status == "dry-run" else "OK"
        print(
            f"{prefix}: device={result.device_id} status={result.status} "
            f"Thing={thing_label} profile={profile_label} rules=[{rule_labels}]"
        )
        if result.status == "created" and result.thing_key:
            print(
                f"NOTICE: Mainflux generated a Thing key for {result.device_id}; "
                "the key is intentionally not printed. Use tools.add_device to store it "
                "in the referenced local environment variable."
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
