from __future__ import annotations

import argparse
from dataclasses import asdict, replace
import json
from pathlib import Path
import sys

from .config import CyberConfig, CyberConfigurationError
from .discovery import NetworkDiscovery
from .fingerprint import ServiceFingerprinter
from .logging_utils import configure_json_logging
from .models import CameraEvent
from .orchestrator import CyberOrchestrator
from .scope_guard import OutOfScopeTargetError, ScopeGuard


def _config_path(value: str | None) -> Path:
    return Path(value) if value else Path(__file__).resolve().parents[2] / "config" / "cyber_agent.yaml"


def _json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=True, indent=2, default=str))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Allowlist-scoped defensive cyber camera agent")
    parser.add_argument("--config", type=Path, help="Cyber YAML config path")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("show-config")
    sub.add_parser("discover")
    for name in ("scan", "audit"):
        command = sub.add_parser(name)
        command.add_argument("--target", required=True, help="Allowlisted IPv4/IPv6 target")
        command.add_argument("--device-class", default="unknown")
    run = sub.add_parser("run")
    run.add_argument("--dry-run", action="store_true", help="Use mock data and suppress all network traffic")
    run.add_argument("--target", help="Optional allowlisted target; omitted runs bounded discovery")
    run.add_argument("--device-class", default="unknown")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = CyberConfig.from_yaml(_config_path(str(args.config) if args.config else None))
        if config.logging.json_logs:
            configure_json_logging(config.logging.file)
        guard = ScopeGuard(config.scope)
        if args.command == "show-config":
            _json(config.safe_dict())
            return 0
        if not config.enabled:
            _json([] if args.command == "discover" else {"state": "DISABLED", "events": []})
            return 0
        if args.command == "discover":
            assets = NetworkDiscovery(config.discovery, guard).discover()
            _json([asdict(asset) for asset in assets])
            return 0
        if args.command == "scan":
            try:
                normalized = guard.require_allowed(args.target)
            except OutOfScopeTargetError:
                print("DENIED_OUTSIDE_SCOPE")
                return 2
            fingerprints = ServiceFingerprinter(config.fingerprint, guard).fingerprint_host(normalized)
            _json([asdict(item) for item in fingerprints])
            return 0
        if args.command == "audit":
            try:
                guard.require_allowed(args.target)
            except OutOfScopeTargetError:
                print("DENIED_OUTSIDE_SCOPE")
                return 2
            orchestrator = CyberOrchestrator.from_config(config)
            report = orchestrator.process(
                CameraEvent(
                    agent_id="cyber-cli",
                    camera_id="cyber-cli",
                    device_class=args.device_class,
                    target_ip=args.target,
                )
            )
            _json(report.to_dict())
            return 0 if report.state in {"DONE", "ERROR"} else 1
        if args.command == "run":
            if args.dry_run:
                config = replace(config, mode="dry-run")
            if args.target:
                try:
                    guard.require_allowed(args.target)
                except OutOfScopeTargetError:
                    print("DENIED_OUTSIDE_SCOPE")
                    return 2
            orchestrator = CyberOrchestrator.from_config(config, dry_run=args.dry_run)
            report = orchestrator.process(
                CameraEvent(
                    agent_id="cyber-cli",
                    camera_id="cyber-cli",
                    device_class=args.device_class,
                    target_ip=args.target,
                )
            )
            _json(report.to_dict())
            return 0
    except (CyberConfigurationError, FileNotFoundError, ValueError) as exc:
        print(f"CONFIG_ERROR: {exc}", file=sys.stderr)
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
