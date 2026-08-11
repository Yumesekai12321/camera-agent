from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping
import os
from pathlib import Path
from typing import Any

from camera_agent.device_registry import (
    DeviceConflictError,
    DeviceRegistry,
    DeviceRegistryError,
    RegistryResult,
)


def remove_device(
    registry: DeviceRegistry,
    device_id: str,
    *,
    remove_local: bool = False,
    confirmed: bool = False,
    dry_run: bool = False,
) -> RegistryResult:
    """Disable by default; explicit removal affects only the local YAML entry."""
    if remove_local and not (confirmed or dry_run):
        raise DeviceConflictError(
            "Local removal requires confirmation; pass --yes after reviewing --dry-run"
        )
    if remove_local:
        return registry.remove_local_device(device_id, dry_run=dry_run)
    return registry.disable_device(device_id, dry_run=dry_run)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Disable a device or explicitly remove only its local manifest entry. "
            "Mainflux, .env, outbox, data and models are never deleted."
        )
    )
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--device-id", required=True)
    parser.add_argument(
        "--remove-local",
        action="store_true",
        help="Remove only the YAML entry; does not remove secrets or remote/history data",
    )
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(
    argv: list[str] | None = None,
    *,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], Any] = print,
    environ: Mapping[str, str] | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    environment = dict(os.environ if environ is None else environ)
    manifest_path = args.manifest or Path(
        environment.get("DEVICES_FILE", "config/devices.yaml")
    )
    registry = DeviceRegistry(manifest_path, args.env_file)
    confirmed = args.yes
    if args.remove_local and not args.dry_run and not confirmed:
        answer = input_fn(
            f"Remove only local manifest entry {args.device_id!r}? "
            "Mainflux/history and .env stay untouched. Type the device ID to confirm: "
        ).strip()
        confirmed = answer == args.device_id
    try:
        result = remove_device(
            registry,
            args.device_id,
            remove_local=args.remove_local,
            confirmed=confirmed,
            dry_run=args.dry_run,
        )
        if result.status == "dry-run":
            action = "remove local entry" if args.remove_local else "disable"
            output_fn(f"DRY-RUN: would {action} for device {args.device_id}")
        elif result.status == "disabled":
            output_fn(f"DISABLED: device {args.device_id}")
        elif result.status == "removed":
            output_fn(
                f"REMOVED LOCAL ENTRY: {args.device_id}; .env and Mainflux/history were untouched"
            )
        else:
            output_fn(f"UNCHANGED: device {args.device_id}")
        return 0
    except DeviceRegistryError as exc:
        output_fn(f"ERROR: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
