from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping
from copy import deepcopy
from functools import partial
from getpass import getpass
import inspect
import os
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from dotenv import dotenv_values

from camera_agent.device_registry import (
    DeviceRegistry,
    DeviceRegistryError,
    RegistryResult,
    generated_env_name,
    validate_device_spec,
)


def build_device_spec(
    *,
    device_id: str,
    display_name: str,
    source_type: str,
    source: Mapping[str, Any],
    mainflux: Mapping[str, Any],
    agent_type: str = "camera",
    pentest: Mapping[str, Any] | None = None,
    enabled: bool = True,
    monitor_roi: list[float] | tuple[float, float, float, float] | None = None,
    process_interval: float = 1.0,
    rules: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the canonical v2 device shape without resolving any secret values."""
    normalized_source_type = source_type.strip().lower()
    normalized_agent_type = agent_type.strip().lower()
    if normalized_source_type == "pentest":
        normalized_agent_type = "pentest"
    source_config = deepcopy(dict(source))
    if normalized_source_type == "tapo_rtsp":
        normalized_source_type = "rtsp"
        source_config.setdefault("adapter", "tapo")
        if "url_env" not in source_config:
            source_config.setdefault("port", 554)
            source_config.setdefault("path", "stream1")
            source_config.setdefault(
                "username_env", generated_env_name(device_id, "rtsp_user")
            )
            source_config.setdefault(
                "password_env", generated_env_name(device_id, "rtsp_pass")
            )
    elif normalized_source_type == "rtsp":
        source_config.setdefault("adapter", "generic")
        if "url_env" not in source_config:
            source_config.setdefault("port", 554)
    elif normalized_source_type == "adb":
        source_config.setdefault("fps", 1.0)
        source_config.setdefault("command_timeout", 6.0)
        source_config.setdefault("require_landscape", False)
        source_config.setdefault("minimum_frame_std", 2.0)
    elif normalized_source_type == "window":
        source_config.setdefault("capture_method", "auto")
        source_config.setdefault("fps", 8.0)
        source_config.setdefault("minimum_frame_std", 2.0)
    elif normalized_source_type == "webcam":
        source_config.setdefault("index", 0)
        source_config.setdefault("backend", "dshow")
    elif normalized_source_type == "pentest":
        source_config.setdefault("ports", [80, 443])
        source_config.setdefault("connect_timeout", 3.0)
        source_config.setdefault("request_timeout", 5.0)
        source_config.setdefault("verify_tls", True)
        source_config.setdefault("allow_public_target", False)
        source_config.setdefault("http_path", "/")

    mainflux_config = deepcopy(dict(mainflux))
    mainflux_config.setdefault("thing_name", f"camera-agent-{device_id}")
    mainflux_config.setdefault(
        "thing_key_env", generated_env_name(device_id, "mainflux_thing_key")
    )
    result = {
        "device_id": device_id,
        "display_name": display_name,
        "enabled": bool(enabled),
        "agent_type": normalized_agent_type,
        "source_type": normalized_source_type,
        "source": source_config,
        "mainflux": mainflux_config,
        "monitor_roi": list(monitor_roi) if monitor_roi is not None else None,
        "process_interval": float(process_interval),
        "rules": deepcopy(dict(rules or {"template": "default", "overrides": {}})),
    }
    if normalized_agent_type == "pentest" and normalized_source_type != "pentest":
        result["pentest"] = deepcopy(dict(pentest or {}))
    return result


def onboard_device(
    registry: DeviceRegistry,
    device: Mapping[str, Any],
    *,
    secret_values: Mapping[str, str] | None = None,
    checker: Callable[[Mapping[str, Any], Mapping[str, str]], Any] | None = None,
    provisioner: Any = None,
    provision_mainflux: bool = False,
    dry_run: bool = False,
    output_fn: Callable[[str], Any] = print,
) -> RegistryResult:
    """Validate/check/provision first, then commit local files as the last step."""
    candidate = deepcopy(dict(device))
    secrets = dict(secret_values or {})
    validate_device_spec(candidate)
    initial_plan = registry.inspect_add(candidate, secrets=secrets)
    if not initial_plan.changed:
        output_fn(f"UNCHANGED: device {candidate['device_id']} is already configured")
        return initial_plan

    if checker is not None and not dry_run and candidate["enabled"]:
        checker(deepcopy(candidate), dict(secrets))

    if provision_mainflux:
        if provisioner is None:
            raise DeviceRegistryError(
                "Mainflux provisioning was requested but no provisioner is available"
            )
        mainflux = candidate["mainflux"]
        key_reference = mainflux.get("thing_key_env")
        thing_key = secrets.get(key_reference) if key_reference else None
        kwargs: dict[str, Any] = {
            "thing_id": mainflux.get("thing_id"),
            "dry_run": dry_run,
        }
        try:
            signature = inspect.signature(provisioner.provision_device)
        except (TypeError, ValueError):
            signature = None
        if signature is None or "thing_key" in signature.parameters:
            kwargs["thing_key"] = thing_key
        if signature is None or "rule_set" in signature.parameters:
            kwargs["rule_set"] = (
                "pentest"
                if candidate.get("agent_type") == "pentest"
                or candidate.get("source_type") == "pentest"
                else "camera"
            )
        provisioned = provisioner.provision_device(
            candidate["device_id"],
            candidate["display_name"],
            **kwargs,
        )
        if not dry_run:
            if getattr(provisioned, "thing_id", None):
                mainflux["thing_id"] = provisioned.thing_id
            provisioned_key = getattr(provisioned, "thing_key", None)
            if provisioned_key:
                if not key_reference:
                    key_reference = generated_env_name(
                        candidate["device_id"], "mainflux_thing_key"
                    )
                    mainflux["thing_key_env"] = key_reference
                secrets[key_reference] = provisioned_key
            registry.inspect_add(candidate, secrets=secrets)

    result = registry.add_device(candidate, secrets=secrets, dry_run=dry_run)
    if result.status == "dry-run":
        output_fn(f"DRY-RUN: would add device {candidate['device_id']}")
    elif result.status == "added":
        output_fn(f"ADDED: device {candidate['device_id']}")
    elif result.status == "updated-secrets":
        output_fn(f"READY: completed local secret references for {candidate['device_id']}")
    else:
        output_fn(f"UNCHANGED: device {candidate['device_id']}")
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Add one camera or bounded pentest source to a version 2 devices manifest."
    )
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--device-id")
    parser.add_argument("--display-name")
    parser.add_argument("--agent-type", choices=("camera", "pentest"), default="camera")
    parser.add_argument(
        "--source-type",
        choices=("rtsp", "tapo_rtsp", "adb", "window", "webcam", "pentest"),
    )
    parser.add_argument("--disabled", action="store_true")
    parser.add_argument("--process-interval", type=float, default=1.0)
    parser.add_argument("--monitor-roi", help="Normalized x,y,width,height")

    parser.add_argument("--host")
    parser.add_argument("--rtsp-port", type=int, default=554)
    parser.add_argument("--rtsp-path")
    parser.add_argument("--rtsp-url-env")
    parser.add_argument("--rtsp-user-env")
    parser.add_argument("--rtsp-pass-env")
    parser.add_argument(
        "--rtsp-no-auth",
        action="store_true",
        help="Declare that a generic RTSP source is intentionally unauthenticated",
    )

    parser.add_argument("--adb-serial")
    parser.add_argument("--adb-serial-env")
    parser.add_argument("--adb-fps", type=float, default=1.0)
    parser.add_argument("--adb-command-timeout", type=float, default=6.0)
    parser.add_argument("--adb-require-landscape", action="store_true")
    parser.add_argument("--minimum-frame-std", type=float, default=2.0)

    parser.add_argument("--window-title")
    parser.add_argument(
        "--window-capture-method",
        choices=("auto", "printwindow", "screen"),
        default="auto",
    )
    parser.add_argument("--window-fps", type=float, default=8.0)

    parser.add_argument("--webcam-index", type=int, default=0)
    parser.add_argument(
        "--webcam-backend", choices=("auto", "dshow", "msmf"), default="dshow"
    )
    parser.add_argument("--webcam-width", type=int)
    parser.add_argument("--webcam-height", type=int)

    parser.add_argument("--pentest-target-host")
    parser.add_argument(
        "--pentest-target-from-source",
        action="store_true",
        help="For a structured RTSP camera, assess the camera host itself",
    )
    parser.add_argument(
        "--pentest-ports",
        help="Finite comma-separated TCP port list, for example 80,443,8080",
    )
    parser.add_argument("--pentest-http-scheme", choices=("http", "https"))
    parser.add_argument("--pentest-http-port", type=int)
    parser.add_argument("--pentest-http-path", default="/")
    parser.add_argument("--pentest-connect-timeout", type=float, default=3.0)
    parser.add_argument("--pentest-request-timeout", type=float, default=5.0)
    parser.add_argument("--pentest-no-tls-verify", action="store_true")
    parser.add_argument("--pentest-allow-public-target", action="store_true")
    parser.add_argument(
        "--pentest-rtsp-probe",
        action="store_true",
        help="Send one unauthenticated, read-only RTSP OPTIONS assessment on port 554",
    )
    parser.add_argument(
        "--pentest-http-options-probe",
        action="store_true",
        help="Issue one read-only HTTP OPTIONS request after the HEAD assessment",
    )
    parser.add_argument(
        "--pentest-tls-assessment",
        action="store_true",
        help="Assess the configured HTTPS certificate lifetime and negotiated TLS version",
    )

    parser.add_argument("--mainflux-thing-id")
    parser.add_argument("--mainflux-thing-name")
    parser.add_argument("--mainflux-thing-key-env")
    parser.add_argument("--mainflux-group-id")
    parser.add_argument("--mainflux-email")
    parser.add_argument("--provision-mainflux", action="store_true")
    parser.add_argument(
        "--skip-source-check",
        action="store_true",
        help="Skip the live one-frame source probe; configuration validation still runs",
    )
    parser.add_argument("--source-check-timeout", type=float, default=12.0)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(
    argv: list[str] | None = None,
    *,
    input_fn: Callable[[str], str] = input,
    getpass_fn: Callable[[str], str] = getpass,
    output_fn: Callable[[str], Any] = print,
    checker: Callable[[Mapping[str, Any], Mapping[str, str]], Any] | None = None,
    provisioner: Any = None,
    environ: Mapping[str, str] | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    environment = dict(os.environ if environ is None else environ)
    manifest_path = args.manifest or Path(
        environment.get("DEVICES_FILE", "config/devices.yaml")
    )
    registry = DeviceRegistry(manifest_path, args.env_file)
    sensitive_values: set[str] = set()

    try:
        device_id = args.device_id or input_fn("Device ID: ").strip()
        display_name = args.display_name or input_fn("Display name: ").strip()
        source_type = args.source_type or input_fn(
            "Source type (rtsp/tapo_rtsp/adb/window/webcam/pentest): "
        ).strip()
        source = _source_from_args(args, source_type, device_id, input_fn)
        agent_type = "pentest" if source_type == "pentest" else args.agent_type
        pentest = (
            _pentest_from_args(args, source_type=source_type, source=source, input_fn=input_fn)
            if agent_type == "pentest" and source_type != "pentest"
            else None
        )
        if args.provision_mainflux and not args.mainflux_group_id and provisioner is None:
            args.mainflux_group_id = input_fn("Mainflux Group ID: ").strip()
        mainflux = {
            key: value
            for key, value in {
                "thing_id": args.mainflux_thing_id,
                "thing_name": args.mainflux_thing_name,
                "thing_key_env": args.mainflux_thing_key_env,
                "group_id": args.mainflux_group_id,
            }.items()
            if value
        }
        device = build_device_spec(
            device_id=device_id,
            display_name=display_name,
            source_type=source_type,
            source=source,
            mainflux=mainflux,
            agent_type=agent_type,
            pentest=pentest,
            enabled=not args.disabled,
            monitor_roi=_parse_roi(args.monitor_roi),
            process_interval=args.process_interval,
        )
        existing_env = _combined_environment(args.env_file, environment)
        secret_updates = _collect_missing_values(
            device,
            existing_env,
            getpass_fn=getpass_fn,
            input_fn=input_fn,
            provision_mainflux=args.provision_mainflux,
        )
        if source_type == "adb" and args.adb_serial:
            serial_reference = device["source"].get("serial_env")
            if serial_reference:
                secret_updates[serial_reference] = args.adb_serial
        sensitive_values.update(value for value in secret_updates.values() if value)
        active_provisioner = provisioner
        if args.provision_mainflux and active_provisioner is None:
            active_provisioner = _create_default_provisioner(
                args,
                environment,
                input_fn=input_fn,
                getpass_fn=getpass_fn,
            )
        active_checker = checker
        if args.skip_source_check:
            active_checker = None
            if device["enabled"] and not args.dry_run:
                output_fn(
                    f"SKIPPED: live source check for {device['device_id']}; "
                    "configuration validation still passed"
                )
        elif active_checker is None:
            from tools.check_device import check_device

            active_checker = partial(
                check_device,
                timeout=args.source_check_timeout,
            )

        onboard_device(
            registry,
            device,
            secret_values=secret_updates,
            checker=active_checker,
            provisioner=active_provisioner,
            provision_mainflux=args.provision_mainflux,
            dry_run=args.dry_run,
            output_fn=output_fn,
        )
        return 0
    except (DeviceRegistryError, OSError, RuntimeError, ValueError) as exc:
        output_fn(f"ERROR: {_redact_message(str(exc), sensitive_values)}")
        return 2


def _source_from_args(
    args: argparse.Namespace,
    source_type: str,
    device_id: str,
    input_fn: Callable[[str], str],
) -> dict[str, Any]:
    if source_type in {"rtsp", "tapo_rtsp"}:
        if source_type == "tapo_rtsp" and args.rtsp_no_auth:
            raise ValueError("--rtsp-no-auth is not valid for the tapo_rtsp preset")
        if args.rtsp_no_auth and (args.rtsp_user_env or args.rtsp_pass_env):
            raise ValueError(
                "--rtsp-no-auth cannot be combined with RTSP credential references"
            )
        if args.rtsp_url_env:
            return {"url_env": args.rtsp_url_env}
        host = args.host or input_fn("RTSP host/IP: ").strip()
        source: dict[str, Any] = {
            "host": host,
            "port": args.rtsp_port,
        }
        path = args.rtsp_path
        if source_type == "rtsp" and not path:
            path = input_fn("RTSP path: ").strip()
        if path:
            source["path"] = path
        if source_type == "tapo_rtsp" or not args.rtsp_no_auth:
            source["username_env"] = args.rtsp_user_env or generated_env_name(
                device_id, "rtsp_user"
            )
            source["password_env"] = args.rtsp_pass_env or generated_env_name(
                device_id, "rtsp_pass"
            )
        return source
    if source_type == "adb":
        serial_env = args.adb_serial_env
        if args.adb_serial and not serial_env:
            serial_env = generated_env_name(device_id, "adb_serial")
        return {
            key: value
            for key, value in {
                "serial_env": serial_env,
                "fps": args.adb_fps,
                "command_timeout": args.adb_command_timeout,
                "require_landscape": args.adb_require_landscape,
                "minimum_frame_std": args.minimum_frame_std,
            }.items()
            if value is not None
        }
    if source_type == "window":
        return {
            "title": args.window_title or input_fn("Window title: ").strip(),
            "capture_method": args.window_capture_method,
            "fps": args.window_fps,
            "minimum_frame_std": args.minimum_frame_std,
        }
    if source_type == "pentest":
        host = args.pentest_target_host or input_fn("Pentest target host/IP: ").strip()
        ports = _parse_ports(
            args.pentest_ports
            or input_fn("TCP ports (comma-separated, max 64): ").strip()
        )
        source = {
            "target_host": host,
            "ports": ports,
            "http_scheme": args.pentest_http_scheme,
            "http_path": args.pentest_http_path,
            "connect_timeout": args.pentest_connect_timeout,
            "request_timeout": args.pentest_request_timeout,
            "verify_tls": not args.pentest_no_tls_verify,
            "allow_public_target": args.pentest_allow_public_target,
            "rtsp_probe": args.pentest_rtsp_probe,
            "http_options_probe": args.pentest_http_options_probe,
            "tls_assessment": args.pentest_tls_assessment,
        }
        if args.pentest_http_port is not None:
            source["http_port"] = args.pentest_http_port
        return {key: value for key, value in source.items() if value is not None}
    return {
        key: value
        for key, value in {
            "index": args.webcam_index,
            "backend": args.webcam_backend,
            "width": args.webcam_width,
            "height": args.webcam_height,
        }.items()
        if value is not None
    }


def _pentest_from_args(
    args: argparse.Namespace,
    *,
    source_type: str,
    source: Mapping[str, Any],
    input_fn: Callable[[str], str],
) -> dict[str, Any]:
    if args.pentest_target_from_source and source_type not in {"rtsp", "tapo_rtsp"}:
        raise ValueError("--pentest-target-from-source requires an RTSP source")
    if args.pentest_target_from_source and source.get("url_env"):
        raise ValueError(
            "--pentest-target-from-source requires structured RTSP host fields"
        )
    host = args.pentest_target_host
    if not host and not args.pentest_target_from_source:
        host = input_fn("Pentest target host/IP: ").strip()
    ports = _parse_ports(
        args.pentest_ports
        or input_fn("TCP ports (comma-separated, max 64): ").strip()
    )
    config: dict[str, Any] = {
        "ports": ports,
        "http_scheme": args.pentest_http_scheme,
        "http_path": args.pentest_http_path,
        "connect_timeout": args.pentest_connect_timeout,
        "request_timeout": args.pentest_request_timeout,
        "verify_tls": not args.pentest_no_tls_verify,
        "allow_public_target": args.pentest_allow_public_target,
        "rtsp_probe": args.pentest_rtsp_probe,
        "http_options_probe": args.pentest_http_options_probe,
        "tls_assessment": args.pentest_tls_assessment,
    }
    if args.pentest_target_from_source:
        config["target_from_source"] = True
    else:
        config["target_host"] = host
    if args.pentest_http_port is not None:
        config["http_port"] = args.pentest_http_port
    return config


def _parse_roi(value: str | None) -> list[float] | None:
    if value is None:
        return None
    try:
        parts = [float(part.strip()) for part in value.split(",")]
    except ValueError as exc:
        raise ValueError("--monitor-roi must be x,y,width,height") from exc
    if len(parts) != 4:
        raise ValueError("--monitor-roi must be x,y,width,height")
    return parts


def _parse_ports(value: str) -> list[int]:
    try:
        ports = [int(part.strip()) for part in value.split(",") if part.strip()]
    except ValueError as exc:
        raise ValueError("--pentest-ports must be comma-separated integers") from exc
    if not ports:
        raise ValueError("--pentest-ports must contain at least one port")
    if len(ports) > 64:
        raise ValueError("--pentest-ports cannot contain more than 64 ports")
    if len(set(ports)) != len(ports) or any(not 1 <= port <= 65535 for port in ports):
        raise ValueError("--pentest-ports must contain unique ports between 1 and 65535")
    return ports


def _combined_environment(
    env_path: Path, environment: Mapping[str, str]
) -> dict[str, str]:
    file_values = {
        key: value
        for key, value in dotenv_values(env_path, interpolate=False).items()
        if value is not None
    }
    return {**file_values, **environment}


def _collect_missing_values(
    device: Mapping[str, Any],
    existing: Mapping[str, str],
    *,
    getpass_fn: Callable[[str], str],
    input_fn: Callable[[str], str],
    provision_mainflux: bool,
) -> dict[str, str]:
    updates: dict[str, str] = {}
    source = device["source"]
    prompts = {
        source.get("username_env"): "Camera username (hidden): ",
        source.get("password_env"): "Camera password (hidden): ",
        source.get("url_env"): "RTSP URL (hidden): ",
    }
    for reference, prompt in prompts.items():
        if not reference:
            continue
        if existing.get(reference):
            updates[reference] = existing[reference]
        else:
            updates[reference] = getpass_fn(prompt)
    serial_reference = source.get("serial_env")
    if serial_reference:
        if existing.get(serial_reference):
            updates[serial_reference] = existing[serial_reference]
        else:
            updates[serial_reference] = input_fn("ADB serial: ").strip()
    key_reference = device["mainflux"].get("thing_key_env")
    if key_reference and existing.get(key_reference):
        updates[key_reference] = existing[key_reference]
    elif key_reference and not provision_mainflux:
        updates[key_reference] = getpass_fn("Mainflux Thing key (hidden): ")
    return updates


def _create_default_provisioner(
    args: argparse.Namespace,
    environment: Mapping[str, str],
    *,
    input_fn: Callable[[str], str],
    getpass_fn: Callable[[str], str],
):
    # Imported only for the explicit provisioning path.
    import requests

    from camera_agent.provisioning import MainfluxProvisioner

    group_id = args.mainflux_group_id or input_fn("Mainflux Group ID: ").strip()
    if not group_id:
        raise DeviceRegistryError("Mainflux Group ID is required for provisioning")
    email = args.mainflux_email or input_fn("Mainflux email: ").strip()
    password = getpass_fn("Mainflux password (hidden, not stored): ")
    base_url = environment.get("MAINFLUX_URL", "http://localhost").strip().rstrip("/")
    verify_tls = environment.get("MAINFLUX_VERIFY_TLS", "true").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }
    session = requests.Session()
    response = session.post(
        urljoin(base_url + "/", "tokens"),
        json={"email": email, "password": password},
        headers={"Content-Type": "application/json"},
        timeout=10,
        verify=verify_tls,
    )
    if not response.ok:
        raise DeviceRegistryError(
            f"Mainflux login failed with HTTP {response.status_code}; credentials were not stored"
        )
    try:
        token = response.json()["token"]
    except (KeyError, TypeError, ValueError) as exc:
        raise DeviceRegistryError("Mainflux login response did not contain a token") from exc
    return MainfluxProvisioner(
        base_url=base_url,
        group_id=group_id,
        session=session,
        verify_tls=verify_tls,
        token=token,
    )


def _redact_message(message: str, sensitive_values: set[str]) -> str:
    redacted = message
    for value in sorted(sensitive_values, key=len, reverse=True):
        if value:
            redacted = redacted.replace(value, "***")
    return redacted


if __name__ == "__main__":
    raise SystemExit(main())
