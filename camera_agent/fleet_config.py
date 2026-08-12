from __future__ import annotations

from dataclasses import dataclass, replace
import os
from pathlib import Path
import re
from typing import Any

import yaml

from .config import (
    ConfigurationError,
    Settings,
    build_mainflux_destination_id,
    parse_normalized_roi,
)
from .rules import RuleConfigurationError, RulesConfig


DEVICE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
ENV_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

ROOT_KEYS = {"version", "fleet", "devices"}
FLEET_KEYS = {"preview"}
DEVICE_KEYS = {
    "device_id",
    "display_name",
    "enabled",
    "source_type",
    "source",
    "mainflux",
    "physical_alarm",
    "monitor_roi",
    "process_interval",
    "rules",
}
MAINFLUX_KEYS = {"thing_id", "group_id", "thing_name", "thing_key_env"}
RULE_KEYS = {"template", "overrides"}
RTSP_SOURCE_KEYS = {
    "adapter",
    "url_env",
    "host",
    "port",
    "path",
    "username_env",
    "password_env",
    "open_timeout",
    "read_timeout",
    "reconnect_initial",
    "reconnect_max",
}
ADB_SOURCE_KEYS = {
    "serial_env",
    "fps",
    "command_timeout",
    "require_landscape",
    "minimum_frame_std",
    "reconnect_initial",
    "reconnect_max",
}
WINDOW_SOURCE_KEYS = {
    "title",
    "capture_method",
    "fps",
    "minimum_frame_std",
    "read_timeout",
    "reconnect_initial",
    "reconnect_max",
}
WEBCAM_SOURCE_KEYS = {
    "index",
    "backend",
    "width",
    "height",
    "read_timeout",
    "reconnect_initial",
    "reconnect_max",
}
PHYSICAL_ALARM_KEYS = {
    "enabled",
    "provider",
    "duration_seconds",
    "cooldown_seconds",
    "audio_id",
}


@dataclass(frozen=True)
class DeviceDefinition:
    device_id: str
    display_name: str
    settings: Settings
    enabled: bool = True
    source_type: str = "rtsp"
    webcam_index: int | None = None
    webcam_backend: str = "dshow"
    webcam_width: int | None = None
    webcam_height: int | None = None
    window_title: str | None = None
    window_capture_method: str = "auto"
    window_fps: float = 8.0
    window_minimum_frame_std: float = 2.0
    adb_serial: str | None = None
    adb_fps: float = 1.0
    adb_command_timeout: float = 6.0
    adb_require_landscape: bool = False
    adb_minimum_frame_std: float = 2.0
    mainflux_thing_id: str | None = None
    mainflux_group_id: str | None = None
    mainflux_thing_name: str | None = None
    mainflux_thing_key_env: str | None = None
    physical_alarm_enabled: bool = False
    physical_alarm_provider: str = "none"
    physical_alarm_duration_seconds: float = 3.0
    physical_alarm_cooldown_seconds: float = 30.0
    physical_alarm_audio_id: int = 8196
    rules_template: str = "default"
    rules_config: RulesConfig | None = None

    @property
    def name(self) -> str:
        """Compatibility alias for existing dashboard/application code."""
        return self.display_name


@dataclass(frozen=True)
class FleetConfig:
    devices: tuple[DeviceDefinition, ...]
    preview: bool = True
    configured_devices: tuple[DeviceDefinition, ...] = ()

    def __post_init__(self) -> None:
        if not self.configured_devices and self.devices:
            object.__setattr__(self, "configured_devices", self.devices)

    @classmethod
    def from_yaml(
        cls,
        path: Path,
        base_settings: Settings,
        *,
        require_mainflux: bool = True,
    ) -> "FleetConfig":
        try:
            with path.open("r", encoding="utf-8") as handle:
                payload = yaml.safe_load(handle) or {}
        except (OSError, yaml.YAMLError) as exc:
            raise ConfigurationError(f"Could not read devices manifest {path}: {exc}") from exc
        if not isinstance(payload, dict):
            raise ConfigurationError("Devices manifest root must be a YAML object")
        _reject_unknown(payload, ROOT_KEYS, "manifest")
        if payload.get("version") != 2 or isinstance(payload.get("version"), bool):
            raise ConfigurationError("Devices manifest version must be 2")

        raw_fleet = payload.get("fleet")
        raw_devices = payload.get("devices")
        if not isinstance(raw_fleet, dict):
            raise ConfigurationError("fleet must be an object")
        if not isinstance(raw_devices, list):
            raise ConfigurationError("devices must be a list")
        _reject_unknown(raw_fleet, FLEET_KEYS, "fleet")
        preview = _as_bool(raw_fleet.get("preview", base_settings.preview), "fleet.preview")

        default_rules = RulesConfig.from_toml(base_settings.rules_file)
        configured: list[DeviceDefinition] = []
        enabled_devices: list[DeviceDefinition] = []
        seen_ids: set[str] = set()
        seen_thing_ids: set[str] = set()
        seen_key_refs: set[str] = set()
        seen_key_values: set[str] = set()

        for index, raw in enumerate(raw_devices, start=1):
            context = f"devices[{index}]"
            if not isinstance(raw, dict):
                raise ConfigurationError(f"{context} must be an object")
            _reject_unknown(raw, DEVICE_KEYS, context)

            device_id = _required_text(raw, "device_id", context)
            if not DEVICE_ID_PATTERN.fullmatch(device_id):
                raise ConfigurationError(
                    f"{context}.device_id must use lowercase letters, numbers, _ or -"
                )
            if device_id in seen_ids:
                raise ConfigurationError(f"Duplicate device id: {device_id}")
            seen_ids.add(device_id)

            display_name = _required_text(raw, "display_name", context)
            if "enabled" not in raw:
                raise ConfigurationError(f"{context}.enabled is required")
            enabled = _as_bool(raw["enabled"], f"{context}.enabled")
            source_type = _required_text(raw, "source_type", context)
            if source_type not in {"rtsp", "adb", "window", "webcam"}:
                raise ConfigurationError(
                    f"{context}.source_type must be rtsp, adb, window or webcam"
                )

            raw_source = raw.get("source")
            if not isinstance(raw_source, dict):
                raise ConfigurationError(f"{context}.source must be an object")
            raw_mainflux = raw.get("mainflux")
            if not isinstance(raw_mainflux, dict):
                raise ConfigurationError(f"{context}.mainflux must be an object")
            _reject_unknown(raw_mainflux, MAINFLUX_KEYS, f"{context}.mainflux")

            thing_key_env = _required_env_reference(
                raw_mainflux.get("thing_key_env"),
                f"{context}.mainflux.thing_key_env",
            )
            if thing_key_env in seen_key_refs:
                raise ConfigurationError(f"Duplicate Mainflux Thing key env: {thing_key_env}")
            seen_key_refs.add(thing_key_env)
            thing_key = _env_value(
                thing_key_env,
                required=enabled and require_mainflux,
                context=f"{context}.mainflux.thing_key_env",
            )
            if thing_key:
                if thing_key in seen_key_values:
                    raise ConfigurationError(
                        "Each configured device must use a different Mainflux Thing key"
                    )
                seen_key_values.add(thing_key)

            thing_id = _optional_text(raw_mainflux.get("thing_id"), f"{context}.mainflux.thing_id")
            if thing_id:
                if thing_id in seen_thing_ids:
                    raise ConfigurationError(f"Duplicate Mainflux Thing id: {thing_id}")
                seen_thing_ids.add(thing_id)
            group_id = _optional_text(raw_mainflux.get("group_id"), f"{context}.mainflux.group_id")
            thing_name = _optional_text(
                raw_mainflux.get("thing_name"),
                f"{context}.mainflux.thing_name",
            )

            interval = _number_value(
                raw.get("process_interval", base_settings.process_interval),
                f"{context}.process_interval",
            )
            if interval < 0.05:
                raise ConfigurationError(f"{context}.process_interval must be at least 0.05")
            roi_value = raw.get("monitor_roi")
            if isinstance(roi_value, list):
                roi_value = ",".join(str(part) for part in roi_value)
            elif roi_value is not None:
                roi_value = str(roi_value)

            device_settings = replace(
                base_settings,
                process_interval=interval,
                monitor_roi=parse_normalized_roi(roi_value),
                mainflux_thing_key=thing_key,
                mainflux_thing_id=thing_id,
                mainflux_destination_id=build_mainflux_destination_id(
                    thing_id=thing_id,
                    thing_key=thing_key,
                    thing_key_env=thing_key_env,
                ),
                preview=False,
                preview_mode="off",
            )
            source_fields: dict[str, Any] = {}
            if source_type == "rtsp":
                device_settings = _resolve_rtsp_source(
                    raw_source,
                    device_settings,
                    context=f"{context}.source",
                    require_environment=enabled,
                )
            elif source_type == "adb":
                device_settings, source_fields = _resolve_adb_source(
                    raw_source,
                    device_settings,
                    context=f"{context}.source",
                    require_environment=enabled,
                )
            elif source_type == "window":
                device_settings, source_fields = _resolve_window_source(
                    raw_source,
                    device_settings,
                    context=f"{context}.source",
                )
            else:
                device_settings, source_fields = _resolve_webcam_source(
                    raw_source,
                    device_settings,
                    context=f"{context}.source",
                )

            alarm_fields = _resolve_physical_alarm(
                raw.get("physical_alarm", {}),
                settings=device_settings,
                source_type=source_type,
                context=f"{context}.physical_alarm",
            )

            raw_rules = raw.get("rules", {})
            if not isinstance(raw_rules, dict):
                raise ConfigurationError(f"{context}.rules must be an object")
            _reject_unknown(raw_rules, RULE_KEYS, f"{context}.rules")
            rules_template = str(raw_rules.get("template", "default")).strip()
            if rules_template != "default":
                raise ConfigurationError(f"{context}.rules.template must be default")
            overrides = raw_rules.get("overrides", {})
            try:
                effective_rules = default_rules.with_overrides(overrides)
            except RuleConfigurationError as exc:
                raise ConfigurationError(f"{context}.rules: {exc}") from exc

            device_settings.validate(
                require_camera=enabled and source_type == "rtsp",
                require_models=False,
                require_mainflux=enabled and require_mainflux,
            )
            definition = DeviceDefinition(
                device_id=device_id,
                display_name=display_name,
                settings=device_settings,
                enabled=enabled,
                source_type=source_type,
                mainflux_thing_id=thing_id,
                mainflux_group_id=group_id,
                mainflux_thing_name=thing_name,
                mainflux_thing_key_env=thing_key_env,
                **alarm_fields,
                rules_template=rules_template,
                rules_config=effective_rules,
                **source_fields,
            )
            configured.append(definition)
            if enabled:
                enabled_devices.append(definition)

        return cls(
            tuple(enabled_devices),
            preview=preview,
            configured_devices=tuple(configured),
        )


def _resolve_rtsp_source(
    raw: dict[str, Any],
    settings: Settings,
    *,
    context: str,
    require_environment: bool,
) -> Settings:
    _reject_unknown(raw, RTSP_SOURCE_KEYS, context)
    adapter = str(raw.get("adapter", "generic")).strip()
    if adapter not in {"generic", "tapo"}:
        raise ConfigurationError(f"{context}.adapter must be generic or tapo")

    url_reference = raw.get("url_env")
    structured_fields = {"host", "port", "path", "username_env", "password_env"}
    if url_reference is not None and any(field in raw for field in structured_fields):
        raise ConfigurationError(
            f"{context}.url_env is mutually exclusive with structured RTSP fields"
        )
    settings = _replace_source_timing(raw, settings, context=context, allow_open_timeout=True)
    if url_reference is not None:
        url_env = _required_env_reference(url_reference, f"{context}.url_env")
        url = _env_value(
            url_env,
            required=require_environment,
            context=f"{context}.url_env",
        )
        return replace(
            settings,
            rtsp_adapter=adapter,
            rtsp_host=None,
            rtsp_username=None,
            rtsp_password=None,
            rtsp_url_override=url,
        )

    host = _required_text(raw, "host", context)
    path_default = "stream1" if adapter == "tapo" else None
    path = _optional_text(raw.get("path", path_default), f"{context}.path")
    if not path:
        raise ConfigurationError(f"{context}.path is required")
    path = path.lstrip("/")
    port = _integer_value(raw.get("port", 554), f"{context}.port")
    if not 1 <= port <= 65535:
        raise ConfigurationError(f"{context}.port must be between 1 and 65535")

    username_env_raw = raw.get("username_env")
    password_env_raw = raw.get("password_env")
    if (username_env_raw is None) != (password_env_raw is None):
        raise ConfigurationError(
            f"{context}.username_env and password_env must be provided together"
        )
    if adapter == "tapo" and username_env_raw is None:
        raise ConfigurationError(
            f"{context}.username_env and password_env are required for the tapo adapter"
        )
    username = None
    password = None
    if username_env_raw is not None:
        username_env = _required_env_reference(username_env_raw, f"{context}.username_env")
        password_env = _required_env_reference(password_env_raw, f"{context}.password_env")
        username = _env_value(
            username_env,
            required=require_environment,
            context=f"{context}.username_env",
        )
        password = _env_value(
            password_env,
            required=require_environment,
            context=f"{context}.password_env",
        )
    return replace(
        settings,
        rtsp_adapter=adapter,
        rtsp_host=host,
        rtsp_username=username,
        rtsp_password=password,
        rtsp_path=path,
        rtsp_port=port,
        rtsp_url_override=None,
    )


def _resolve_adb_source(
    raw: dict[str, Any],
    settings: Settings,
    *,
    context: str,
    require_environment: bool,
) -> tuple[Settings, dict[str, Any]]:
    _reject_unknown(raw, ADB_SOURCE_KEYS, context)
    serial = None
    if raw.get("serial_env") is not None:
        serial_env = _required_env_reference(raw["serial_env"], f"{context}.serial_env")
        serial = _env_value(
            serial_env,
            required=require_environment,
            context=f"{context}.serial_env",
        )
    fps = _number_value(raw.get("fps", 1.0), f"{context}.fps")
    command_timeout = _number_value(
        raw.get("command_timeout", 6.0),
        f"{context}.command_timeout",
    )
    minimum_frame_std = _number_value(
        raw.get("minimum_frame_std", 2.0),
        f"{context}.minimum_frame_std",
    )
    if not 0.2 <= fps <= 5:
        raise ConfigurationError(f"{context}.fps must be between 0.2 and 5")
    if not 1 <= command_timeout <= 30:
        raise ConfigurationError(f"{context}.command_timeout must be between 1 and 30")
    if not 0 <= minimum_frame_std <= 50:
        raise ConfigurationError(f"{context}.minimum_frame_std must be between 0 and 50")
    settings = _replace_source_timing(raw, settings, context=context)
    return settings, {
        "adb_serial": serial,
        "adb_fps": fps,
        "adb_command_timeout": command_timeout,
        "adb_require_landscape": _as_bool(
            raw.get("require_landscape", False),
            f"{context}.require_landscape",
        ),
        "adb_minimum_frame_std": minimum_frame_std,
    }


def _resolve_window_source(
    raw: dict[str, Any],
    settings: Settings,
    *,
    context: str,
) -> tuple[Settings, dict[str, Any]]:
    _reject_unknown(raw, WINDOW_SOURCE_KEYS, context)
    title = _required_text(raw, "title", context)
    method = str(raw.get("capture_method", "auto")).strip()
    if method not in {"auto", "printwindow", "screen"}:
        raise ConfigurationError(
            f"{context}.capture_method must be auto, printwindow or screen"
        )
    fps = _number_value(raw.get("fps", 8.0), f"{context}.fps")
    minimum_frame_std = _number_value(
        raw.get("minimum_frame_std", 2.0),
        f"{context}.minimum_frame_std",
    )
    if not 0.5 <= fps <= 30:
        raise ConfigurationError(f"{context}.fps must be between 0.5 and 30")
    if not 0 <= minimum_frame_std <= 50:
        raise ConfigurationError(f"{context}.minimum_frame_std must be between 0 and 50")
    settings = _replace_source_timing(raw, settings, context=context)
    return settings, {
        "window_title": title,
        "window_capture_method": method,
        "window_fps": fps,
        "window_minimum_frame_std": minimum_frame_std,
    }


def _resolve_webcam_source(
    raw: dict[str, Any],
    settings: Settings,
    *,
    context: str,
) -> tuple[Settings, dict[str, Any]]:
    _reject_unknown(raw, WEBCAM_SOURCE_KEYS, context)
    index = _integer_value(raw.get("index", 0), f"{context}.index")
    if not 0 <= index <= 32:
        raise ConfigurationError(f"{context}.index must be between 0 and 32")
    backend = str(raw.get("backend", "dshow")).strip()
    if backend not in {"auto", "dshow", "msmf"}:
        raise ConfigurationError(f"{context}.backend must be auto, dshow or msmf")
    width = _optional_positive_integer(raw.get("width"), f"{context}.width")
    height = _optional_positive_integer(raw.get("height"), f"{context}.height")
    settings = _replace_source_timing(raw, settings, context=context)
    return settings, {
        "webcam_index": index,
        "webcam_backend": backend,
        "webcam_width": width,
        "webcam_height": height,
    }


def _resolve_physical_alarm(
    raw: Any,
    *,
    settings: Settings,
    source_type: str,
    context: str,
) -> dict[str, Any]:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigurationError(f"{context} must be an object")
    _reject_unknown(raw, PHYSICAL_ALARM_KEYS, context)
    enabled = _as_bool(raw.get("enabled", False), f"{context}.enabled")
    provider = str(raw.get("provider", "tapo" if enabled else "none")).strip()
    if provider not in {"none", "tapo"}:
        raise ConfigurationError(f"{context}.provider must be none or tapo")
    duration = _number_value(raw.get("duration_seconds", 3.0), f"{context}.duration_seconds")
    cooldown = _number_value(raw.get("cooldown_seconds", 30.0), f"{context}.cooldown_seconds")
    audio_id = _integer_value(raw.get("audio_id", 8196), f"{context}.audio_id")
    if duration <= 0:
        raise ConfigurationError(f"{context}.duration_seconds must be greater than zero")
    if cooldown < duration:
        raise ConfigurationError(
            f"{context}.cooldown_seconds must be at least duration_seconds"
        )
    if audio_id <= 0:
        raise ConfigurationError(f"{context}.audio_id must be greater than zero")
    if enabled and provider == "none":
        raise ConfigurationError(f"{context}.provider is required when enabled")
    if enabled and provider == "tapo":
        if source_type != "rtsp" or settings.rtsp_adapter != "tapo":
            raise ConfigurationError(
                f"{context}.provider=tapo requires a Tapo RTSP device"
            )
        if settings.rtsp_url_override:
            raise ConfigurationError(
                f"{context}.provider=tapo requires structured host and env credentials"
            )
        if not (settings.rtsp_host and settings.rtsp_username and settings.rtsp_password):
            raise ConfigurationError(
                f"{context}.provider=tapo requires host, username_env and password_env"
            )
    return {
        "physical_alarm_enabled": enabled,
        "physical_alarm_provider": provider,
        "physical_alarm_duration_seconds": duration,
        "physical_alarm_cooldown_seconds": cooldown,
        "physical_alarm_audio_id": audio_id,
    }


def _replace_source_timing(
    raw: dict[str, Any],
    settings: Settings,
    *,
    context: str,
    allow_open_timeout: bool = False,
) -> Settings:
    replacements: dict[str, float] = {}
    mapping = {
        "read_timeout": "camera_read_timeout",
        "reconnect_initial": "reconnect_initial",
        "reconnect_max": "reconnect_max",
    }
    if allow_open_timeout:
        mapping["open_timeout"] = "camera_open_timeout"
    for source_name, setting_name in mapping.items():
        if source_name in raw:
            value = _number_value(raw[source_name], f"{context}.{source_name}")
            if value <= 0:
                raise ConfigurationError(f"{context}.{source_name} must be greater than zero")
            replacements[setting_name] = value
    candidate = replace(settings, **replacements) if replacements else settings
    if candidate.reconnect_max < candidate.reconnect_initial:
        raise ConfigurationError(
            f"{context}.reconnect_max cannot be lower than reconnect_initial"
        )
    return candidate


def _reject_unknown(raw: dict[str, Any], allowed: set[str], context: str) -> None:
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ConfigurationError(f"{context} has unknown field: {unknown[0]}")


def _required_text(raw: dict[str, Any], field: str, context: str) -> str:
    if field not in raw:
        raise ConfigurationError(f"{context}.{field} is required")
    value = _optional_text(raw[field], f"{context}.{field}")
    if value is None:
        raise ConfigurationError(f"{context}.{field} cannot be empty")
    return value


def _optional_text(value: Any, context: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigurationError(f"{context} must be text")
    stripped = value.strip()
    if not stripped:
        raise ConfigurationError(f"{context} cannot be empty")
    return stripped


def _required_env_reference(value: Any, context: str) -> str:
    name = _optional_text(value, context)
    if name is None or not ENV_NAME_PATTERN.fullmatch(name):
        raise ConfigurationError(f"{context} must be a valid environment variable name")
    return name


def _env_value(name: str, *, required: bool, context: str) -> str | None:
    value = os.getenv(name)
    if value is None or not value.strip():
        if required:
            raise ConfigurationError(f"Environment variable referenced by {context} is empty")
        return None
    return value.strip()


def _as_bool(value: Any, field: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "yes", "1", "on"}:
            return True
        if normalized in {"false", "no", "0", "off"}:
            return False
    raise ConfigurationError(f"{field} must be true or false")


def _number_value(value: Any, field: str) -> float:
    if isinstance(value, bool):
        raise ConfigurationError(f"{field} must be a number")
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"{field} must be a number") from exc


def _integer_value(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise ConfigurationError(f"{field} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"{field} must be an integer") from exc
    if isinstance(value, float) and not value.is_integer():
        raise ConfigurationError(f"{field} must be an integer")
    return parsed


def _optional_positive_integer(value: Any, field: str) -> int | None:
    if value is None:
        return None
    parsed = _integer_value(value, field)
    if parsed < 1:
        raise ConfigurationError(f"{field} must be at least 1")
    return parsed
