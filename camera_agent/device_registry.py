from __future__ import annotations

from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from io import StringIO
import math
import os
from pathlib import Path
import re
import tempfile
from typing import Any

from dotenv import dotenv_values
import yaml

from .rules import (
    CameraOfflineRuleConfig,
    FacebookRuleConfig,
    RuleConfigurationError,
    RulesConfig,
)
from .pentest import validate_target_host, validate_target_ports
from .features import FeatureId
from .person_guard import PersonGuardConfig


DEVICE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
ENV_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
ENV_LINE_PATTERN = re.compile(
    r"^(?P<prefix>\s*(?:export\s+)?)(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*="
)
RTSP_WITH_CREDENTIALS_PATTERN = re.compile(r"(?i)rtsp://[^/\s]*@")

ROOT_FIELDS = {"version", "fleet", "devices"}
FLEET_FIELDS = {"preview"}
COMMON_DEVICE_FIELDS = {
    "device_id",
    "display_name",
    "enabled",
    "agent_type",
    "source_type",
    "source",
    "mainflux",
    "pentest",
    "physical_alarm",
    "features",
    "control",
    "ptz",
    "auto_patrol",
    "monitor_roi",
    "process_interval",
    "rules",
}
SOURCE_FIELDS = {
    "rtsp": {
        "adapter",
        "host",
        "port",
        "path",
        "username_env",
        "password_env",
        "url_env",
        "open_timeout",
        "read_timeout",
        "reconnect_initial",
        "reconnect_max",
    },
    "adb": {
        "serial_env",
        "fps",
        "command_timeout",
        "require_landscape",
        "minimum_frame_std",
        "reconnect_initial",
        "reconnect_max",
    },
    "window": {
        "title",
        "capture_method",
        "fps",
        "minimum_frame_std",
        "read_timeout",
        "reconnect_initial",
        "reconnect_max",
    },
    "webcam": {
        "index",
        "backend",
        "width",
        "height",
        "read_timeout",
        "reconnect_initial",
        "reconnect_max",
    },
    "pentest": {
        "target_host",
        "ports",
        "http_scheme",
        "http_port",
        "http_path",
        "connect_timeout",
        "request_timeout",
        "verify_tls",
        "allow_public_target",
        "rtsp_probe",
        "http_options_probe",
        "tls_assessment",
    },
}
MAINFLUX_FIELDS = {
    "thing_id", "thing_name", "thing_key_env", "group_id", "channel_id", "control_channel_id"
}
RULE_FIELDS = {"template", "overrides"}
PTZ_FIELDS = {"enabled", "provider", "port", "profile_token", "velocity", "move_duration_seconds"}
AUTO_PATROL_FIELDS = {
    "enabled", "observe_seconds", "search_move_interval_seconds", "max_alarm_events_per_screen"
}
FEATURES_FIELDS = {"available", "default", "person_guard"}
PERSON_GUARD_FIELDS = {
    "model_path_env",
    "model_sha256_env",
    "minimum_confidence",
    "confirmation_frames",
    "absence_rearm_seconds",
    "tracking_dead_zone",
    "tracking_move_duration_seconds",
    "tracking_move_interval_seconds",
}
CONTROL_FIELDS = {"preview_url_env", "preview_token_env"}
PENTEST_FIELDS = {
    "target_host",
    "target_from_source",
    "ports",
    "http_scheme",
    "http_port",
    "http_path",
    "connect_timeout",
    "request_timeout",
    "verify_tls",
    "allow_public_target",
    "rtsp_probe",
    "http_options_probe",
    "tls_assessment",
}
DEFAULT_RULES_CONFIG = RulesConfig(
    facebook=FacebookRuleConfig(),
    camera_offline=CameraOfflineRuleConfig(),
)
LITERAL_SECRET_FIELDS = {
    "camera_account",
    "key",
    "pass",
    "password",
    "rtsp_url",
    "secret",
    "thing_key",
    "token",
    "user",
    "username",
}


class DeviceRegistryError(ValueError):
    """Base error for safe local device-registry operations."""


class RegistryValidationError(DeviceRegistryError):
    pass


class DeviceConflictError(DeviceRegistryError):
    pass


class DeviceNotFoundError(DeviceRegistryError):
    pass


class RegistryCommitError(DeviceRegistryError):
    pass


@dataclass(frozen=True)
class RegistryResult:
    status: str
    changed: bool
    device_id: str
    device: dict[str, Any] | None = None


def generated_env_name(device_id: str, purpose: str) -> str:
    """Return a deterministic, non-secret environment-variable reference."""
    _validate_device_id(device_id)
    normalized_purpose = purpose.strip().lower()
    if not re.fullmatch(r"[a-z0-9_]+", normalized_purpose):
        raise RegistryValidationError("environment-variable purpose is invalid")
    normalized_id = re.sub(r"[^A-Z0-9]", "_", device_id.upper())
    return f"CAMERA_AGENT_{normalized_id}_{normalized_purpose.upper()}"


def validate_manifest(
    payload: Mapping[str, Any],
    *,
    env_values: Mapping[str, str | None] | None = None,
) -> None:
    if not isinstance(payload, Mapping):
        raise RegistryValidationError("Devices manifest root must be a YAML object")
    unknown_root = set(payload) - ROOT_FIELDS
    if unknown_root:
        raise RegistryValidationError(
            f"Devices manifest has unsupported fields: {', '.join(sorted(unknown_root))}"
        )
    if payload.get("version") != 2:
        raise RegistryValidationError("Devices manifest version must be 2")
    fleet = payload.get("fleet")
    if not isinstance(fleet, Mapping):
        raise RegistryValidationError("Devices manifest fleet must be an object")
    unknown_fleet = set(fleet) - FLEET_FIELDS
    if unknown_fleet:
        raise RegistryValidationError(
            f"Devices manifest fleet has unsupported fields: {', '.join(sorted(unknown_fleet))}"
        )
    if "preview" in fleet and not isinstance(fleet["preview"], bool):
        raise RegistryValidationError("Devices manifest fleet.preview must be boolean")
    devices = payload.get("devices")
    if not isinstance(devices, list):
        raise RegistryValidationError("Devices manifest devices must be a list")

    seen_ids: set[str] = set()
    seen_references: dict[str, str] = {}
    seen_thing_ids: dict[str, str] = {}
    seen_thing_names: dict[str, str] = {}
    seen_key_values: dict[str, str] = {}
    resolved = env_values or {}

    for index, device in enumerate(devices, start=1):
        validate_device_spec(device, location=f"devices[{index}]")
        device_id = device["device_id"]
        if device_id in seen_ids:
            raise RegistryValidationError(f"Duplicate device_id: {device_id}")
        seen_ids.add(device_id)

        for reference in _environment_references(device):
            owner = seen_references.get(reference)
            if owner is not None:
                raise RegistryValidationError(
                    f"Duplicate environment reference {reference} for {owner} and {device_id}"
                )
            seen_references[reference] = device_id

        mainflux = device.get("mainflux", {})
        thing_id = _optional_text(mainflux.get("thing_id"))
        if thing_id:
            owner = seen_thing_ids.get(thing_id)
            if owner is not None:
                raise RegistryValidationError(
                    f"Duplicate Mainflux Thing ID for {owner} and {device_id}"
                )
            seen_thing_ids[thing_id] = device_id
        thing_name = _optional_text(mainflux.get("thing_name"))
        if thing_name:
            owner = seen_thing_names.get(thing_name)
            if owner is not None:
                raise RegistryValidationError(
                    f"Duplicate Mainflux Thing name for {owner} and {device_id}"
                )
            seen_thing_names[thing_name] = device_id

        key_reference = _optional_text(mainflux.get("thing_key_env"))
        key_value = resolved.get(key_reference) if key_reference else None
        if key_value:
            owner = seen_key_values.get(key_value)
            if owner is not None:
                raise RegistryValidationError(
                    f"Duplicate resolved Mainflux Thing key for {owner} and {device_id}"
                )
            seen_key_values[key_value] = device_id


def validate_device_spec(device: Any, *, location: str = "device") -> None:
    if not isinstance(device, Mapping):
        raise RegistryValidationError(f"{location} must be a YAML object")
    _reject_literal_secrets(device, location)
    unknown = set(device) - COMMON_DEVICE_FIELDS
    if unknown:
        raise RegistryValidationError(
            f"{location} has unsupported fields: {', '.join(sorted(unknown))}"
        )

    device_id = device.get("device_id")
    if not isinstance(device_id, str):
        raise RegistryValidationError(f"{location}.device_id is required")
    _validate_device_id(device_id, location=f"{location}.device_id")

    display_name = device.get("display_name")
    if not isinstance(display_name, str) or not display_name.strip():
        raise RegistryValidationError(f"{location}.display_name cannot be empty")
    if len(display_name.strip()) > 128:
        raise RegistryValidationError(f"{location}.display_name is too long")
    if not isinstance(device.get("enabled"), bool):
        raise RegistryValidationError(f"{location}.enabled must be true or false")

    agent_type = device.get("agent_type", "camera")
    if agent_type not in {"camera", "pentest"}:
        raise RegistryValidationError(
            f"{location}.agent_type must be camera or pentest"
        )

    source_type = device.get("source_type")
    if source_type not in SOURCE_FIELDS:
        raise RegistryValidationError(
            f"{location}.source_type must be rtsp, adb, window, webcam or pentest"
        )
    source = device.get("source")
    if not isinstance(source, Mapping):
        raise RegistryValidationError(f"{location}.source must be an object")
    unknown_source = set(source) - SOURCE_FIELDS[source_type]
    if unknown_source:
        raise RegistryValidationError(
            f"{location}.source has unsupported {source_type} fields: "
            + ", ".join(sorted(unknown_source))
        )
    _validate_source(source_type, source, location=f"{location}.source")
    if source_type == "pentest":
        agent_type = "pentest"
    pentest = device.get("pentest")
    if agent_type == "pentest" and source_type != "pentest":
        if not isinstance(pentest, Mapping):
            raise RegistryValidationError(
                f"{location}.pentest must be an object for a pentest agent"
            )
        _validate_pentest_config(
            pentest,
            source_type=source_type,
            source=source,
            location=f"{location}.pentest",
        )
    elif pentest is not None:
        raise RegistryValidationError(
            f"{location}.pentest requires agent_type: pentest"
        )

    mainflux = device.get("mainflux")
    if not isinstance(mainflux, Mapping):
        raise RegistryValidationError(f"{location}.mainflux must be an object")
    unknown_mainflux = set(mainflux) - MAINFLUX_FIELDS
    if unknown_mainflux:
        raise RegistryValidationError(
            f"{location}.mainflux has unsupported fields: "
            + ", ".join(sorted(unknown_mainflux))
        )
    for field in ("thing_id", "thing_name", "group_id", "channel_id", "control_channel_id"):
        value = mainflux.get(field)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise RegistryValidationError(f"{location}.mainflux.{field} cannot be empty")
    if "thing_key_env" not in mainflux:
        raise RegistryValidationError(f"{location}.mainflux.thing_key_env is required")
    _validate_env_reference(
        mainflux.get("thing_key_env"), f"{location}.mainflux.thing_key_env"
    )

    roi = device.get("monitor_roi")
    if roi is not None:
        if not isinstance(roi, (list, tuple)) or len(roi) != 4:
            raise RegistryValidationError(
                f"{location}.monitor_roi must contain x, y, width, height"
            )
        try:
            x, y, width, height = (float(value) for value in roi)
        except (TypeError, ValueError) as exc:
            raise RegistryValidationError(
                f"{location}.monitor_roi must contain numbers"
            ) from exc
        if not all(math.isfinite(value) for value in (x, y, width, height)):
            raise RegistryValidationError(f"{location}.monitor_roi must be finite")
        if min(x, y, width, height) < 0 or width <= 0 or height <= 0:
            raise RegistryValidationError(f"{location}.monitor_roi values are invalid")
        if x + width > 1 or y + height > 1:
            raise RegistryValidationError(f"{location}.monitor_roi must remain within 0..1")

    interval = device.get("process_interval")
    if isinstance(interval, bool) or not isinstance(interval, (int, float)):
        raise RegistryValidationError(f"{location}.process_interval must be a number")
    if not math.isfinite(float(interval)) or float(interval) < 0.05:
        raise RegistryValidationError(
            f"{location}.process_interval must be at least 0.05"
        )

    rules = device.get("rules")
    if not isinstance(rules, Mapping):
        raise RegistryValidationError(f"{location}.rules must be an object")
    unknown_rules = set(rules) - RULE_FIELDS
    if unknown_rules:
        raise RegistryValidationError(
            f"{location}.rules has unsupported fields: {', '.join(sorted(unknown_rules))}"
        )
    template = rules.get("template")
    if not isinstance(template, str) or not template.strip():
        raise RegistryValidationError(f"{location}.rules.template cannot be empty")
    if template != "default":
        raise RegistryValidationError(f"{location}.rules.template must be default")
    overrides = rules.get("overrides")
    if not isinstance(overrides, dict):
        raise RegistryValidationError(f"{location}.rules.overrides must be an object")
    try:
        effective_rules = DEFAULT_RULES_CONFIG.with_overrides(overrides)
    except RuleConfigurationError as exc:
        raise RegistryValidationError(f"{location}.rules: {exc}") from exc
    ptz = device.get("ptz", {})
    _validate_ptz(ptz, source_type=source_type, source=source, location=f"{location}.ptz")
    auto_patrol = device.get("auto_patrol", {})
    _validate_auto_patrol(
        auto_patrol,
        ptz_enabled=bool(ptz.get("enabled", False)) if isinstance(ptz, Mapping) else False,
        rules=effective_rules,
        agent_type=agent_type,
        location=f"{location}.auto_patrol",
    )
    _validate_features(device.get("features"), agent_type=agent_type, location=f"{location}.features")
    _validate_control(device.get("control"), location=f"{location}.control")


class _ManifestLock:
    def __init__(self, manifest_path: Path) -> None:
        self.path = manifest_path.with_name(f".{manifest_path.name}.lock")
        self._descriptor: int | None = None

    def __enter__(self) -> "_ManifestLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._descriptor = os.open(
                self.path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
            os.write(self._descriptor, str(os.getpid()).encode("ascii"))
        except FileExistsError as exc:
            raise DeviceConflictError(
                f"Device manifest is locked by another operation: {self.path}"
            ) from exc
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        if self._descriptor is not None:
            os.close(self._descriptor)
            self._descriptor = None
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


class DeviceRegistry:
    def __init__(
        self,
        manifest_path: Path | str,
        env_path: Path | str | None = None,
        *,
        replace_func: Callable[[str | os.PathLike[str], str | os.PathLike[str]], None]
        | None = None,
    ) -> None:
        self.manifest_path = Path(manifest_path)
        self.env_path = Path(env_path) if env_path is not None else None
        self._replace = replace_func or os.replace

    def load_manifest(self) -> dict[str, Any]:
        payload = self._read_manifest()
        validate_manifest(payload, env_values=self._read_env_values())
        return deepcopy(payload)

    def inspect_add(
        self,
        device: Mapping[str, Any],
        *,
        secrets: Mapping[str, str] | None = None,
    ) -> RegistryResult:
        manifest = self._read_manifest()
        env_bytes = self._read_env_bytes()
        _, _, status, changed = self._prepare_add(
            manifest,
            env_bytes,
            device,
            secrets or {},
        )
        return RegistryResult(status, changed, str(device.get("device_id") or ""), deepcopy(device))

    def add_device(
        self,
        device: Mapping[str, Any],
        *,
        secrets: Mapping[str, str] | None = None,
        dry_run: bool = False,
    ) -> RegistryResult:
        secret_updates = dict(secrets or {})
        if dry_run:
            plan = self.inspect_add(device, secrets=secret_updates)
            return RegistryResult(
                "dry-run",
                plan.changed,
                plan.device_id,
                deepcopy(dict(device)),
            )

        with _ManifestLock(self.manifest_path):
            original_manifest = self._read_manifest_bytes()
            original_env = self._read_env_bytes()
            manifest = self._parse_manifest_bytes(original_manifest)
            candidate, candidate_env, status, changed = self._prepare_add(
                manifest,
                original_env,
                device,
                secret_updates,
            )
            if not changed:
                return RegistryResult(status, False, device["device_id"], deepcopy(dict(device)))
            self._commit(
                self._render_manifest(candidate),
                candidate_env if candidate_env != original_env else None,
                original_manifest,
                original_env,
            )
            return RegistryResult(status, True, device["device_id"], deepcopy(dict(device)))

    def disable_device(self, device_id: str, *, dry_run: bool = False) -> RegistryResult:
        return self._change_device_state(device_id, remove=False, dry_run=dry_run)

    def remove_local_device(
        self, device_id: str, *, dry_run: bool = False
    ) -> RegistryResult:
        return self._change_device_state(device_id, remove=True, dry_run=dry_run)

    def _change_device_state(
        self,
        device_id: str,
        *,
        remove: bool,
        dry_run: bool,
    ) -> RegistryResult:
        _validate_device_id(device_id)

        def prepare(payload: dict[str, Any]) -> tuple[dict[str, Any], RegistryResult]:
            validate_manifest(payload, env_values=self._read_env_values())
            candidate = deepcopy(payload)
            devices = candidate["devices"]
            index = next(
                (i for i, item in enumerate(devices) if item["device_id"] == device_id),
                None,
            )
            if index is None:
                return candidate, RegistryResult("unchanged", False, device_id, None)
            existing = deepcopy(devices[index])
            if remove:
                devices.pop(index)
                status = "removed"
            elif not devices[index]["enabled"]:
                return candidate, RegistryResult("unchanged", False, device_id, existing)
            else:
                devices[index]["enabled"] = False
                status = "disabled"
            validate_manifest(candidate, env_values=self._read_env_values())
            return candidate, RegistryResult(status, True, device_id, existing)

        if dry_run:
            candidate, result = prepare(self._read_manifest())
            del candidate
            return RegistryResult("dry-run", result.changed, device_id, result.device)

        with _ManifestLock(self.manifest_path):
            original = self._read_manifest_bytes()
            candidate, result = prepare(self._parse_manifest_bytes(original))
            if not result.changed:
                return result
            self._commit(self._render_manifest(candidate), None, original, self._read_env_bytes())
            return result

    def _prepare_add(
        self,
        manifest: dict[str, Any],
        env_bytes: bytes,
        device: Mapping[str, Any],
        secrets: Mapping[str, str],
    ) -> tuple[dict[str, Any], bytes, str, bool]:
        env_values = _parse_env_values(env_bytes)
        validate_manifest(manifest, env_values=env_values)
        validate_device_spec(device)
        candidate = deepcopy(manifest)
        candidate_device = deepcopy(dict(device))
        existing = next(
            (
                item
                for item in candidate["devices"]
                if item["device_id"] == candidate_device["device_id"]
            ),
            None,
        )
        is_new = existing is None
        if existing is not None and existing != candidate_device:
            raise DeviceConflictError(
                f"Device {candidate_device['device_id']} already exists with "
                "different configuration; "
                "use an explicit update workflow"
            )
        if is_new:
            candidate["devices"].append(candidate_device)

        referenced = set(_environment_references(candidate_device))
        for name, value in secrets.items():
            _validate_env_reference(name, "secret update environment variable")
            if name not in referenced:
                raise RegistryValidationError(
                    f"Secret update {name} is not referenced by device "
                    f"{candidate_device['device_id']}"
                )
            _validate_secret_value(value)
            current = env_values.get(name)
            if current not in {None, "", value}:
                raise DeviceConflictError(
                    f"Environment variable {name} already has a different value; "
                    "secret rotation must be explicit"
                )
            env_values[name] = value

        validate_manifest(candidate, env_values=env_values)
        candidate_env = _render_env_updates(env_bytes, secrets)
        env_changed = candidate_env != env_bytes
        if not is_new and not env_changed:
            return candidate, candidate_env, "unchanged", False
        status = "added" if is_new else "updated-secrets"
        return candidate, candidate_env, status, True

    def _read_manifest(self) -> dict[str, Any]:
        return self._parse_manifest_bytes(self._read_manifest_bytes())

    def _read_manifest_bytes(self) -> bytes:
        try:
            return self.manifest_path.read_bytes()
        except FileNotFoundError:
            return b"version: 2\nfleet:\n  preview: true\ndevices: []\n"
        except OSError as exc:
            raise RegistryValidationError(
                f"Could not read devices manifest {self.manifest_path}: {exc}"
            ) from exc

    def _parse_manifest_bytes(self, content: bytes) -> dict[str, Any]:
        try:
            payload = yaml.safe_load(content.decode("utf-8")) or {}
        except (UnicodeDecodeError, yaml.YAMLError) as exc:
            raise RegistryValidationError(
                f"Could not parse devices manifest {self.manifest_path}: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            raise RegistryValidationError("Devices manifest root must be a YAML object")
        return payload

    def _read_env_bytes(self) -> bytes:
        if self.env_path is None:
            return b""
        try:
            return self.env_path.read_bytes()
        except FileNotFoundError:
            return b""
        except OSError as exc:
            raise RegistryValidationError(f"Could not read environment file: {exc}") from exc

    def _read_env_values(self) -> dict[str, str | None]:
        return _parse_env_values(self._read_env_bytes())

    @staticmethod
    def _render_manifest(payload: Mapping[str, Any]) -> bytes:
        return yaml.safe_dump(
            dict(payload),
            allow_unicode=True,
            default_flow_style=False,
            sort_keys=False,
        ).encode("utf-8")

    def _commit(
        self,
        manifest_bytes: bytes,
        env_bytes: bytes | None,
        original_manifest: bytes,
        original_env: bytes,
    ) -> None:
        manifest_existed = self.manifest_path.exists()
        env_existed = self.env_path.exists() if self.env_path is not None else False
        stages: list[Path] = []
        committed: list[tuple[Path, bytes, bool]] = []
        try:
            env_stage = None
            if env_bytes is not None:
                if self.env_path is None:
                    raise RegistryValidationError(
                        "Cannot persist secret values without an environment file"
                    )
                env_stage = _stage_bytes(self.env_path, env_bytes, private=True)
                stages.append(env_stage)
            manifest_stage = _stage_bytes(self.manifest_path, manifest_bytes)
            stages.append(manifest_stage)

            if env_stage is not None and self.env_path is not None:
                self._replace(env_stage, self.env_path)
                stages.remove(env_stage)
                committed.append((self.env_path, original_env, env_existed))
            self._replace(manifest_stage, self.manifest_path)
            stages.remove(manifest_stage)
            committed.append((self.manifest_path, original_manifest, manifest_existed))
        except Exception as exc:
            rollback_errors: list[str] = []
            for path, original, existed in reversed(committed):
                try:
                    if existed:
                        restore_stage = _stage_bytes(
                            path,
                            original,
                            private=path == self.env_path,
                        )
                        try:
                            os.replace(restore_stage, path)
                        finally:
                            if restore_stage.exists():
                                restore_stage.unlink()
                    else:
                        path.unlink(missing_ok=True)
                except OSError:
                    rollback_errors.append(str(path))
            detail = (
                " Rollback could not restore: " + ", ".join(rollback_errors)
                if rollback_errors
                else ""
            )
            raise RegistryCommitError(
                f"Could not commit device registry transaction.{detail}"
            ) from exc
        finally:
            for stage in stages:
                try:
                    stage.unlink()
                except FileNotFoundError:
                    pass


def _validate_device_id(value: str, *, location: str = "device_id") -> None:
    if not isinstance(value, str) or not DEVICE_ID_PATTERN.fullmatch(value):
        raise RegistryValidationError(
            f"{location} must use lowercase letters, numbers, _ or - (maximum 63 characters)"
        )


def _validate_env_reference(value: Any, location: str) -> None:
    if not isinstance(value, str) or not ENV_NAME_PATTERN.fullmatch(value):
        raise RegistryValidationError(
            f"{location} must be a valid environment variable name"
        )


def _validate_pentest_config(
    pentest: Mapping[str, Any],
    *,
    source_type: str,
    source: Mapping[str, Any],
    location: str,
) -> None:
    unknown = set(pentest) - PENTEST_FIELDS
    if unknown:
        raise RegistryValidationError(
            f"{location} has unsupported fields: {', '.join(sorted(unknown))}"
        )
    target_from_source = pentest.get("target_from_source", False)
    if not isinstance(target_from_source, bool):
        raise RegistryValidationError(f"{location}.target_from_source must be boolean")
    target_host = pentest.get("target_host")
    if target_from_source:
        if target_host is not None:
            raise RegistryValidationError(
                f"{location}.target_host and target_from_source are mutually exclusive"
            )
        if source_type != "rtsp" or source.get("url_env") or not source.get("host"):
            raise RegistryValidationError(
                f"{location}.target_from_source requires structured RTSP host"
            )
        target_host = source.get("host")
    if target_host is None:
        raise RegistryValidationError(
            f"{location}.target_host is required unless target_from_source is true"
        )
    try:
        validate_target_host(target_host)
    except ValueError as exc:
        raise RegistryValidationError(f"{location}.target_host is invalid: {exc}") from exc

    raw_ports = pentest.get("ports")
    if isinstance(raw_ports, str):
        try:
            raw_ports = [int(part.strip()) for part in raw_ports.split(",") if part.strip()]
        except ValueError as exc:
            raise RegistryValidationError(f"{location}.ports must contain integers") from exc
    if not isinstance(raw_ports, (list, tuple)):
        raise RegistryValidationError(f"{location}.ports must be a list")
    try:
        validated_ports = validate_target_ports(raw_ports)
    except ValueError as exc:
        raise RegistryValidationError(f"{location}.ports: {exc}") from exc

    http_scheme = pentest.get("http_scheme")
    if http_scheme not in {None, "http", "https"}:
        raise RegistryValidationError(
            f"{location}.http_scheme must be http, https or omitted"
        )
    if http_scheme is not None:
        http_port = pentest.get("http_port", 443 if http_scheme == "https" else 80)
        if isinstance(http_port, bool) or not isinstance(http_port, int) or not 1 <= http_port <= 65535:
            raise RegistryValidationError(f"{location}.http_port must be 1..65535")
        if http_port not in validated_ports:
            raise RegistryValidationError(
                f"{location}.http_port must be included in the bounded ports list"
            )
    http_path = pentest.get("http_path", "/")
    if (
        not isinstance(http_path, str)
        or not http_path.startswith("/")
        or "\r" in http_path
        or "\n" in http_path
        or len(http_path) > 2048
    ):
        raise RegistryValidationError(f"{location}.http_path must be a safe absolute path")
    for field in ("connect_timeout", "request_timeout"):
        if field in pentest:
            _number_in_range(pentest[field], 0.1, 30.0, f"{location}.{field}")
    for field in ("verify_tls", "allow_public_target"):
        if field in pentest and not isinstance(pentest[field], bool):
            raise RegistryValidationError(f"{location}.{field} must be boolean")
    for field in ("rtsp_probe", "http_options_probe", "tls_assessment"):
        if field in pentest and not isinstance(pentest[field], bool):
            raise RegistryValidationError(f"{location}.{field} must be boolean")
    if pentest.get("rtsp_probe", False):
        if 554 not in validated_ports:
            raise RegistryValidationError(
                f"{location}.rtsp_probe requires port 554 in the bounded ports list"
            )
    if pentest.get("http_options_probe", False) and http_scheme is None:
        raise RegistryValidationError(f"{location}.http_options_probe requires http_scheme")
    if pentest.get("tls_assessment", False) and http_scheme != "https":
        raise RegistryValidationError(
            f"{location}.tls_assessment requires http_scheme: https"
        )


def _validate_ptz(
    ptz: Any,
    *,
    source_type: str,
    source: Mapping[str, Any],
    location: str,
) -> None:
    if ptz is None:
        ptz = {}
    if not isinstance(ptz, Mapping):
        raise RegistryValidationError(f"{location} must be an object")
    unknown = set(ptz) - PTZ_FIELDS
    if unknown:
        raise RegistryValidationError(f"{location} has unsupported fields: {', '.join(sorted(unknown))}")
    enabled = ptz.get("enabled", False)
    if not isinstance(enabled, bool):
        raise RegistryValidationError(f"{location}.enabled must be boolean")
    provider = ptz.get("provider", "onvif" if enabled else "none")
    if provider not in {"none", "onvif"}:
        raise RegistryValidationError(f"{location}.provider must be none or onvif")
    port = ptz.get("port", 2020)
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise RegistryValidationError(f"{location}.port must be 1..65535")
    token = ptz.get("profile_token")
    if token is not None and (not isinstance(token, str) or not token.strip()):
        raise RegistryValidationError(f"{location}.profile_token cannot be empty")
    _number_in_range(ptz.get("velocity", 0.35), 0.000001, 1.0, f"{location}.velocity")
    _number_in_range(ptz.get("move_duration_seconds", 0.7), 0.1, 5.0, f"{location}.move_duration_seconds")
    if enabled:
        if provider != "onvif":
            raise RegistryValidationError(f"{location}.provider=onvif is required when enabled")
        if source_type != "rtsp" or "url_env" in source:
            raise RegistryValidationError(f"{location}.provider=onvif requires structured RTSP source")
        if not source.get("host") or not source.get("username_env") or not source.get("password_env"):
            raise RegistryValidationError(f"{location}.provider=onvif requires camera account references")


def _validate_auto_patrol(
    auto_patrol: Any,
    *,
    ptz_enabled: bool,
    rules: RulesConfig,
    agent_type: str,
    location: str,
) -> None:
    if auto_patrol is None:
        auto_patrol = {}
    if not isinstance(auto_patrol, Mapping):
        raise RegistryValidationError(f"{location} must be an object")
    unknown = set(auto_patrol) - AUTO_PATROL_FIELDS
    if unknown:
        raise RegistryValidationError(f"{location} has unsupported fields: {', '.join(sorted(unknown))}")
    enabled = auto_patrol.get("enabled", False)
    if not isinstance(enabled, bool):
        raise RegistryValidationError(f"{location}.enabled must be boolean")
    _number_in_range(auto_patrol.get("observe_seconds", 5.0), 1.0, 120.0, f"{location}.observe_seconds")
    _number_in_range(
        auto_patrol.get("search_move_interval_seconds", 1.0), 0.1, 30.0,
        f"{location}.search_move_interval_seconds",
    )
    alarms = auto_patrol.get("max_alarm_events_per_screen", 3)
    if isinstance(alarms, bool) or not isinstance(alarms, int) or not 1 <= alarms <= 10:
        raise RegistryValidationError(f"{location}.max_alarm_events_per_screen must be 1..10")
    if enabled and agent_type != "camera":
        raise RegistryValidationError(f"{location} requires agent_type: camera")
    if enabled and not ptz_enabled:
        raise RegistryValidationError(f"{location}.enabled requires ptz.enabled: true")
    if enabled and rules.facebook.cooldown_seconds != 3:
        raise RegistryValidationError(f"{location} requires facebook_usage.cooldown_seconds: 3")


def _validate_features(value: Any, *, agent_type: str, location: str) -> None:
    if value is None:
        return
    if not isinstance(value, Mapping):
        raise RegistryValidationError(f"{location} must be an object")
    unknown = set(value) - FEATURES_FIELDS
    if unknown:
        raise RegistryValidationError(f"{location} has unsupported fields: {', '.join(sorted(unknown))}")
    available = value.get("available", [FeatureId.FACEBOOK_MONITOR.value])
    if not isinstance(available, list) or not available:
        raise RegistryValidationError(f"{location}.available must be a non-empty list")
    normalized: set[FeatureId] = set()
    for index, raw in enumerate(available, start=1):
        if not isinstance(raw, str):
            raise RegistryValidationError(f"{location}.available[{index}] must be text")
        try:
            feature = FeatureId(raw.strip())
        except ValueError as exc:
            raise RegistryValidationError(f"{location}.available[{index}] is unsupported") from exc
        if feature == FeatureId.NONE:
            raise RegistryValidationError(f"{location}.available cannot contain none")
        if feature in normalized:
            raise RegistryValidationError(f"{location}.available contains a duplicate feature")
        normalized.add(feature)
    default = value.get("default", FeatureId.FACEBOOK_MONITOR.value)
    if not isinstance(default, str):
        raise RegistryValidationError(f"{location}.default must be text")
    try:
        default_feature = FeatureId(default.strip())
    except ValueError as exc:
        raise RegistryValidationError(f"{location}.default is unsupported") from exc
    if default_feature not in normalized:
        raise RegistryValidationError(f"{location}.default must be present in available")
    person = value.get("person_guard")
    if FeatureId.PERSON_GUARD not in normalized:
        if person is not None:
            raise RegistryValidationError(f"{location}.person_guard requires person_guard in available")
        return
    if agent_type != "camera":
        raise RegistryValidationError(f"{location}.person_guard requires agent_type: camera")
    if not isinstance(person, Mapping):
        raise RegistryValidationError(f"{location}.person_guard is required when person_guard is available")
    unknown_person = set(person) - PERSON_GUARD_FIELDS
    if unknown_person:
        raise RegistryValidationError(
            f"{location}.person_guard has unsupported fields: {', '.join(sorted(unknown_person))}"
        )
    _validate_env_reference(person.get("model_path_env"), f"{location}.person_guard.model_path_env")
    _validate_env_reference(person.get("model_sha256_env"), f"{location}.person_guard.model_sha256_env")
    try:
        PersonGuardConfig(
            minimum_confidence=float(person.get("minimum_confidence", 0.45)),
            confirmation_frames=int(person.get("confirmation_frames", 2)),
            absence_rearm_seconds=float(person.get("absence_rearm_seconds", 3.0)),
            tracking_dead_zone=float(person.get("tracking_dead_zone", 0.15)),
            tracking_move_duration_seconds=float(person.get("tracking_move_duration_seconds", 0.25)),
            tracking_move_interval_seconds=float(person.get("tracking_move_interval_seconds", 0.5)),
        ).validate()
    except (TypeError, ValueError) as exc:
        raise RegistryValidationError(f"{location}.person_guard is invalid: {exc}") from exc


def _validate_control(value: Any, *, location: str) -> None:
    if value is None:
        return
    if not isinstance(value, Mapping):
        raise RegistryValidationError(f"{location} must be an object")
    unknown = set(value) - CONTROL_FIELDS
    if unknown:
        raise RegistryValidationError(f"{location} has unsupported fields: {', '.join(sorted(unknown))}")
    url_env = value.get("preview_url_env")
    token_env = value.get("preview_token_env")
    if url_env is None and token_env is None:
        return
    _validate_env_reference(url_env, f"{location}.preview_url_env")
    _validate_env_reference(token_env, f"{location}.preview_token_env")


def _validate_source(source_type: str, source: Mapping[str, Any], *, location: str) -> None:
    if source_type == "rtsp":
        adapter = source.get("adapter", "generic")
        if adapter not in {"generic", "tapo"}:
            raise RegistryValidationError(f"{location}.adapter must be generic or tapo")
        url_env = source.get("url_env")
        if url_env is not None:
            _validate_env_reference(url_env, f"{location}.url_env")
            conflicting = {
                field
                for field in ("host", "port", "path", "username_env", "password_env")
                if field in source
            }
            if conflicting:
                raise RegistryValidationError(
                    f"{location}.url_env cannot be combined with structured RTSP fields"
                )
        else:
            host = source.get("host")
            if (
                not isinstance(host, str)
                or not host.strip()
                or any(token in host for token in ("://", "@"))
                or any(character.isspace() for character in host)
            ):
                raise RegistryValidationError(f"{location}.host is invalid")
            port = source.get("port", 554)
            if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
                raise RegistryValidationError(f"{location}.port must be 1..65535")
            path = source.get("path", "stream1" if adapter == "tapo" else "")
            if (
                not isinstance(path, str)
                or not path.strip()
                or "://" in path
                or "@" in path
            ):
                raise RegistryValidationError(f"{location}.path is invalid")
            username_env = source.get("username_env")
            password_env = source.get("password_env")
            if (username_env is None) != (password_env is None):
                raise RegistryValidationError(
                    f"{location}.username_env and password_env must be provided together"
                )
            if username_env is not None:
                _validate_env_reference(username_env, f"{location}.username_env")
                _validate_env_reference(password_env, f"{location}.password_env")
            if adapter == "tapo" and username_env is None:
                raise RegistryValidationError(
                    f"{location}.username_env and password_env are required for tapo"
                )
        _validate_timing(source, location, allow_open_timeout=True)
        return

    if source_type == "adb":
        serial_env = source.get("serial_env")
        if serial_env is not None:
            _validate_env_reference(serial_env, f"{location}.serial_env")
        _number_in_range(source.get("fps", 1.0), 0.2, 5.0, f"{location}.fps")
        _number_in_range(
            source.get("command_timeout", 6.0),
            1.0,
            30.0,
            f"{location}.command_timeout",
        )
        _number_in_range(
            source.get("minimum_frame_std", 2.0),
            0.0,
            50.0,
            f"{location}.minimum_frame_std",
        )
        if not isinstance(source.get("require_landscape", False), bool):
            raise RegistryValidationError(f"{location}.require_landscape must be boolean")
        _validate_timing(source, location)
        return

    if source_type == "window":
        title = source.get("title")
        if not isinstance(title, str) or not title.strip():
            raise RegistryValidationError(f"{location}.title is required")
        if source.get("capture_method", "auto") not in {"auto", "printwindow", "screen"}:
            raise RegistryValidationError(
                f"{location}.capture_method must be auto, printwindow or screen"
            )
        _number_in_range(source.get("fps", 8.0), 0.5, 30.0, f"{location}.fps")
        _number_in_range(
            source.get("minimum_frame_std", 2.0),
            0.0,
            50.0,
            f"{location}.minimum_frame_std",
        )
        _validate_timing(source, location)
        return

    if source_type == "pentest":
        try:
            validate_target_host(source.get("target_host"))
        except ValueError as exc:
            raise RegistryValidationError(f"{location}.target_host is invalid: {exc}") from exc
        raw_ports = source.get("ports")
        if isinstance(raw_ports, str):
            try:
                raw_ports = [int(part.strip()) for part in raw_ports.split(",") if part.strip()]
            except ValueError as exc:
                raise RegistryValidationError(f"{location}.ports must contain integers") from exc
        if not isinstance(raw_ports, (list, tuple)):
            raise RegistryValidationError(f"{location}.ports must be a list")
        try:
            validate_target_ports(raw_ports)
        except ValueError as exc:
            raise RegistryValidationError(f"{location}.ports: {exc}") from exc
        http_scheme = source.get("http_scheme")
        if http_scheme not in {None, "http", "https"}:
            raise RegistryValidationError(
                f"{location}.http_scheme must be http, https or omitted"
            )
        if http_scheme is not None:
            http_port = source.get("http_port", 443 if http_scheme == "https" else 80)
            if isinstance(http_port, bool) or not isinstance(http_port, int) or not 1 <= http_port <= 65535:
                raise RegistryValidationError(f"{location}.http_port must be 1..65535")
            if http_port not in raw_ports:
                raise RegistryValidationError(
                    f"{location}.http_port must be included in the bounded ports list"
                )
        http_path = source.get("http_path", "/")
        if (
            not isinstance(http_path, str)
            or not http_path.startswith("/")
            or "\r" in http_path
            or "\n" in http_path
            or len(http_path) > 2048
        ):
            raise RegistryValidationError(f"{location}.http_path must be a safe absolute path")
        for field in ("connect_timeout", "request_timeout"):
            if field in source:
                _number_in_range(source[field], 0.1, 30.0, f"{location}.{field}")
        for field in ("verify_tls", "allow_public_target"):
            if field in source and not isinstance(source[field], bool):
                raise RegistryValidationError(f"{location}.{field} must be boolean")
        for field in ("rtsp_probe", "http_options_probe", "tls_assessment"):
            if field in source and not isinstance(source[field], bool):
                raise RegistryValidationError(f"{location}.{field} must be boolean")
        if source.get("rtsp_probe") and 554 not in raw_ports:
            raise RegistryValidationError(
                f"{location}.rtsp_probe requires port 554 in the bounded ports list"
            )
        if source.get("http_options_probe") and http_scheme is None:
            raise RegistryValidationError(f"{location}.http_options_probe requires http_scheme")
        if source.get("tls_assessment") and http_scheme != "https":
            raise RegistryValidationError(
                f"{location}.tls_assessment requires http_scheme: https"
            )
        return

    index = source.get("index", 0)
    if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index <= 32:
        raise RegistryValidationError(f"{location}.index must be between 0 and 32")
    if source.get("backend", "dshow") not in {"auto", "dshow", "msmf"}:
        raise RegistryValidationError(f"{location}.backend must be auto, dshow or msmf")
    for field in ("width", "height"):
        value = source.get(field)
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value < 1
        ):
            raise RegistryValidationError(f"{location}.{field} must be a positive integer")
    _validate_timing(source, location)


def _validate_timing(
    source: Mapping[str, Any], location: str, *, allow_open_timeout: bool = False
) -> None:
    fields = ["read_timeout", "reconnect_initial", "reconnect_max"]
    if allow_open_timeout:
        fields.append("open_timeout")
    for field in fields:
        if field not in source:
            continue
        value = source[field]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise RegistryValidationError(f"{location}.{field} must be a number")
        if not math.isfinite(float(value)) or float(value) <= 0:
            raise RegistryValidationError(f"{location}.{field} must be greater than zero")
    initial = source.get("reconnect_initial")
    maximum = source.get("reconnect_max")
    if initial is not None and maximum is not None and float(maximum) < float(initial):
        raise RegistryValidationError(
            f"{location}.reconnect_max cannot be lower than reconnect_initial"
        )


def _number_in_range(value: Any, minimum: float, maximum: float, location: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RegistryValidationError(f"{location} must be a number")
    parsed = float(value)
    if not math.isfinite(parsed) or not minimum <= parsed <= maximum:
        raise RegistryValidationError(f"{location} must be between {minimum} and {maximum}")


def _reject_literal_secrets(value: Any, location: str) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = str(key).strip().lower()
            child_location = f"{location}.{key}"
            if not normalized.endswith("_env") and normalized in LITERAL_SECRET_FIELDS:
                raise RegistryValidationError(
                    f"{child_location} looks like a literal secret; use an *_env reference"
                )
            _reject_literal_secrets(child, child_location)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_literal_secrets(child, f"{location}[{index}]")
    elif isinstance(value, str) and RTSP_WITH_CREDENTIALS_PATTERN.search(value):
        raise RegistryValidationError(
            f"{location} contains an RTSP URL with literal credentials; use url_env"
        )


def _environment_references(device: Mapping[str, Any]) -> list[str]:
    references: list[str] = []
    source = device.get("source", {})
    mainflux = device.get("mainflux", {})
    for field in ("username_env", "password_env", "url_env", "serial_env"):
        value = source.get(field)
        if isinstance(value, str) and value:
            references.append(value)
    value = mainflux.get("thing_key_env")
    if isinstance(value, str) and value:
        references.append(value)
    features = device.get("features", {})
    if isinstance(features, Mapping):
        person = features.get("person_guard", {})
        if isinstance(person, Mapping):
            for field in ("model_path_env", "model_sha256_env"):
                value = person.get(field)
                if isinstance(value, str) and value:
                    references.append(value)
    control = device.get("control", {})
    if isinstance(control, Mapping):
        for field in ("preview_url_env", "preview_token_env"):
            value = control.get(field)
            if isinstance(value, str) and value:
                references.append(value)
    return references


def _optional_text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _validate_secret_value(value: Any) -> None:
    if not isinstance(value, str) or not value:
        raise RegistryValidationError("Secret values cannot be empty")
    if any(character in value for character in ("\x00", "\r", "\n")):
        raise RegistryValidationError("Secret values cannot contain NUL or newlines")
    if "${" in value:
        raise RegistryValidationError(
            "Secret values containing ${ cannot be safely stored in python-dotenv"
        )


def _parse_env_values(content: bytes) -> dict[str, str | None]:
    if not content:
        return {}
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RegistryValidationError("Environment file must be UTF-8") from exc
    parsed = dotenv_values(stream=StringIO(text), interpolate=False)
    return dict(parsed)


def _render_env_updates(original: bytes, updates: Mapping[str, str]) -> bytes:
    if not updates:
        return original
    try:
        text = original.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RegistryValidationError("Environment file must be UTF-8") from exc
    lines = text.splitlines(keepends=True)
    indexes: dict[str, int] = {}
    for index, line in enumerate(lines):
        match = ENV_LINE_PATTERN.match(line)
        if match is None:
            continue
        name = match.group("name")
        if name in indexes:
            raise RegistryValidationError(
                f"Environment file contains duplicate variable {name}"
            )
        indexes[name] = index

    current = _parse_env_values(original)
    newline = "\r\n" if "\r\n" in text else "\n"
    for name, value in updates.items():
        if current.get(name) == value:
            continue
        rendered = f"{name}='{value.replace(chr(39), chr(92) + chr(39))}'{newline}"
        if name in indexes:
            lines[indexes[name]] = rendered
        else:
            if lines and not lines[-1].endswith(("\n", "\r")):
                lines[-1] += newline
            indexes[name] = len(lines)
            lines.append(rendered)
    return "".join(lines).encode("utf-8")


def _stage_bytes(path: Path, content: bytes, *, private: bool = False) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_path = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    stage = Path(raw_path)
    try:
        if private:
            try:
                os.chmod(stage, 0o600)
            except OSError:
                pass
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        stage.unlink(missing_ok=True)
        raise
    return stage
