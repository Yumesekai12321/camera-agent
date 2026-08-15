from __future__ import annotations

from dataclasses import dataclass, field, replace
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
from .pentest import validate_target_host, validate_target_ports
from .features import FeatureId
from .person_guard import PersonGuardConfig
from .rules import RuleConfigurationError, RulesConfig


DEVICE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
ENV_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

ROOT_KEYS = {"version", "fleet", "devices"}
FLEET_KEYS = {"preview"}
DEVICE_KEYS = {
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
PENTEST_KEYS = {
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
MAINFLUX_KEYS = {
    "thing_id", "group_id", "thing_name", "thing_key_env", "channel_id", "control_channel_id"
}
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
PENTEST_SOURCE_KEYS = {
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
PHYSICAL_ALARM_KEYS = {
    "enabled",
    "provider",
    "duration_seconds",
    "cooldown_seconds",
    "audio_id",
}
PTZ_KEYS = {
    "enabled", "provider", "port", "profile_token", "velocity", "move_duration_seconds"
}
AUTO_PATROL_KEYS = {
    "enabled", "observe_seconds", "search_move_interval_seconds", "max_alarm_events_per_screen"
}
FEATURES_KEYS = {"available", "default", "person_guard"}
PERSON_GUARD_KEYS = {
    "model_path_env",
    "model_sha256_env",
    "minimum_confidence",
    "confirmation_frames",
    "absence_rearm_seconds",
    "tracking_dead_zone",
    "tracking_move_duration_seconds",
    "tracking_move_interval_seconds",
    "search_move_interval_seconds",
    "search_move_duration_seconds",
}
CONTROL_KEYS = {"preview_url_env", "preview_token_env"}


@dataclass(frozen=True)
class DeviceDefinition:
    device_id: str
    display_name: str
    settings: Settings
    enabled: bool = True
    agent_type: str = "camera"
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
    mainflux_channel_id: str | None = None
    mainflux_control_channel_id: str | None = None
    physical_alarm_enabled: bool = False
    physical_alarm_provider: str = "none"
    physical_alarm_duration_seconds: float = 3.0
    physical_alarm_cooldown_seconds: float = 30.0
    physical_alarm_audio_id: int = 8196
    ptz_enabled: bool = False
    ptz_provider: str = "none"
    ptz_port: int = 2020
    ptz_profile_token: str | None = None
    ptz_velocity: float = 0.35
    ptz_move_duration_seconds: float = 0.7
    auto_patrol_enabled: bool = False
    auto_patrol_observe_seconds: float = 5.0
    auto_patrol_search_move_interval_seconds: float = 1.0
    auto_patrol_max_alarm_events_per_screen: int = 3
    feature_allowed: frozenset[FeatureId] = frozenset({FeatureId.FACEBOOK_MONITOR})
    feature_default: FeatureId = FeatureId.FACEBOOK_MONITOR
    person_model_path: Path | None = None
    person_model_sha256: str | None = field(default=None, repr=False)
    person_guard_config: PersonGuardConfig = PersonGuardConfig()
    control_preview_url: str | None = None
    control_preview_token: str | None = field(default=None, repr=False)
    rules_template: str = "default"
    rules_config: RulesConfig | None = None
    pentest_target_host: str | None = None
    pentest_ports: tuple[int, ...] = ()
    pentest_http_scheme: str | None = None
    pentest_http_port: int | None = None
    pentest_http_path: str = "/"
    pentest_connect_timeout: float = 3.0
    pentest_request_timeout: float = 5.0
    pentest_verify_tls: bool = True
    pentest_allow_public_target: bool = False
    pentest_rtsp_probe: bool = False
    pentest_http_options_probe: bool = False
    pentest_tls_assessment: bool = False

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
            agent_type = str(raw.get("agent_type", "camera")).strip().lower()
            if agent_type not in {"camera", "pentest"}:
                raise ConfigurationError(
                    f"{context}.agent_type must be camera or pentest"
                )
            source_type = _required_text(raw, "source_type", context)
            if source_type not in {"rtsp", "adb", "window", "webcam", "pentest"}:
                raise ConfigurationError(
                    f"{context}.source_type must be rtsp, adb, window, webcam or pentest"
                )
            if source_type == "pentest":
                # Backward-compatible read of the first implementation. New
                # manifests should keep the camera source in `source` and put
                # pentest settings in the separate `pentest` block.
                agent_type = "pentest"

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
            control_channel_id = _optional_text(
                raw_mainflux.get("control_channel_id"),
                f"{context}.mainflux.control_channel_id",
            )
            channel_id = _optional_text(
                raw_mainflux.get("channel_id"), f"{context}.mainflux.channel_id"
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
                mainflux_messages_path=(
                    f"/http/channels/{channel_id}/messages"
                    if channel_id
                    else base_settings.mainflux_messages_path
                ),
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
            elif source_type == "webcam":
                device_settings, source_fields = _resolve_webcam_source(
                    raw_source,
                    device_settings,
                    context=f"{context}.source",
                )
            else:
                device_settings, source_fields = _resolve_pentest_source(
                    raw_source,
                    device_settings,
                    context=f"{context}.source",
                )

            pentest_fields: dict[str, Any] = {}
            if agent_type == "pentest" and source_type != "pentest":
                raw_pentest = raw.get("pentest")
                if not isinstance(raw_pentest, dict):
                    raise ConfigurationError(
                        f"{context}.pentest must be an object for a pentest agent"
                    )
                device_settings, pentest_fields = _resolve_pentest_config(
                    raw_pentest,
                    source_type=source_type,
                    source=raw_source,
                    settings=device_settings,
                    context=f"{context}.pentest",
                )
            elif agent_type == "camera" and "pentest" in raw:
                raise ConfigurationError(
                    f"{context}.pentest requires agent_type: pentest"
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
            ptz_fields = _resolve_ptz(
                raw.get("ptz", {}),
                settings=device_settings,
                source_type=source_type,
                context=f"{context}.ptz",
            )
            auto_patrol_fields = _resolve_auto_patrol(
                raw.get("auto_patrol", {}),
                ptz_enabled=ptz_fields["ptz_enabled"],
                rules=effective_rules,
                context=f"{context}.auto_patrol",
            )
            if agent_type != "camera" and auto_patrol_fields["auto_patrol_enabled"]:
                raise ConfigurationError(f"{context}.auto_patrol requires agent_type: camera")
            feature_fields = _resolve_features(
                raw.get("features"), context=f"{context}.features"
            )
            if agent_type != "camera" and FeatureId.PERSON_GUARD in feature_fields["feature_allowed"]:
                raise ConfigurationError(f"{context}.features.person_guard requires agent_type: camera")
            control_fields = _resolve_control(
                raw.get("control"), context=f"{context}.control"
            )

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
                agent_type=agent_type,
                source_type=source_type,
                mainflux_thing_id=thing_id,
                mainflux_group_id=group_id,
                mainflux_thing_name=thing_name,
                mainflux_thing_key_env=thing_key_env,
                mainflux_channel_id=channel_id,
                mainflux_control_channel_id=control_channel_id,
                **alarm_fields,
                **ptz_fields,
                **auto_patrol_fields,
                **feature_fields,
                **control_fields,
                rules_template=rules_template,
                rules_config=effective_rules,
                **source_fields,
                **pentest_fields,
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


def _resolve_pentest_config(
    raw: dict[str, Any],
    *,
    source_type: str,
    source: dict[str, Any],
    settings: Settings,
    context: str,
) -> tuple[Settings, dict[str, Any]]:
    _reject_unknown(raw, PENTEST_KEYS, context)
    target_from_source = _as_bool(
        raw.get("target_from_source", False), f"{context}.target_from_source"
    )
    target_host = raw.get("target_host")
    if target_from_source:
        if target_host is not None:
            raise ConfigurationError(
                f"{context}.target_host and target_from_source are mutually exclusive"
            )
        if source_type != "rtsp" or source.get("url_env") or not source.get("host"):
            raise ConfigurationError(
                f"{context}.target_from_source requires structured RTSP host"
            )
        target_host = source["host"]
    if target_host is None:
        raise ConfigurationError(
            f"{context}.target_host is required unless target_from_source is true"
        )
    effective = dict(raw)
    effective["target_host"] = target_host
    return _resolve_pentest_source(effective, settings, context=context)


def _resolve_pentest_source(
    raw: dict[str, Any],
    settings: Settings,
    *,
    context: str,
) -> tuple[Settings, dict[str, Any]]:
    _reject_unknown(raw, PENTEST_SOURCE_KEYS, context)
    try:
        target_host = validate_target_host(_required_text(raw, "target_host", context))
    except ValueError as exc:
        raise ConfigurationError(f"{context}.target_host is invalid: {exc}") from exc

    raw_ports = raw.get("ports")
    if isinstance(raw_ports, str):
        parts = [part.strip() for part in raw_ports.split(",") if part.strip()]
        try:
            ports = tuple(int(part) for part in parts)
        except ValueError as exc:
            raise ConfigurationError(f"{context}.ports must contain integers") from exc
    elif isinstance(raw_ports, list):
        ports = tuple(raw_ports)
    else:
        raise ConfigurationError(f"{context}.ports must be a list or comma-separated text")
    try:
        ports = validate_target_ports(ports)
    except ValueError as exc:
        raise ConfigurationError(f"{context}.ports: {exc}") from exc

    http_scheme_raw = raw.get("http_scheme")
    http_scheme = None if http_scheme_raw is None else str(http_scheme_raw).strip().lower()
    if http_scheme not in {None, "http", "https"}:
        raise ConfigurationError(f"{context}.http_scheme must be http, https or omitted")
    http_port = None
    if http_scheme is not None:
        http_port = _integer_value(
            raw.get("http_port", 443 if http_scheme == "https" else 80),
            f"{context}.http_port",
        )
        if not 1 <= http_port <= 65535:
            raise ConfigurationError(f"{context}.http_port must be between 1 and 65535")
        if http_port not in ports:
            raise ConfigurationError(
                f"{context}.http_port must be included in the bounded ports list"
            )
    http_path = str(raw.get("http_path", "/")).strip()
    if not http_path.startswith("/") or "\r" in http_path or "\n" in http_path or len(http_path) > 2048:
        raise ConfigurationError(f"{context}.http_path must be a safe absolute path")
    connect_timeout = _number_value(
        raw.get("connect_timeout", 3.0), f"{context}.connect_timeout"
    )
    request_timeout = _number_value(
        raw.get("request_timeout", 5.0), f"{context}.request_timeout"
    )
    if not 0.1 <= connect_timeout <= 30:
        raise ConfigurationError(f"{context}.connect_timeout must be between 0.1 and 30")
    if not 0.1 <= request_timeout <= 30:
        raise ConfigurationError(f"{context}.request_timeout must be between 0.1 and 30")
    verify_tls = _as_bool(raw.get("verify_tls", True), f"{context}.verify_tls")
    allow_public_target = _as_bool(
        raw.get("allow_public_target", False), f"{context}.allow_public_target"
    )
    rtsp_probe = _as_bool(raw.get("rtsp_probe", False), f"{context}.rtsp_probe")
    if rtsp_probe and 554 not in ports:
        raise ConfigurationError(
            f"{context}.rtsp_probe requires port 554 in the bounded ports list"
        )
    http_options_probe = _as_bool(
        raw.get("http_options_probe", False), f"{context}.http_options_probe"
    )
    if http_options_probe and http_scheme is None:
        raise ConfigurationError(
            f"{context}.http_options_probe requires http_scheme"
        )
    tls_assessment = _as_bool(
        raw.get("tls_assessment", False), f"{context}.tls_assessment"
    )
    if tls_assessment and http_scheme != "https":
        raise ConfigurationError(
            f"{context}.tls_assessment requires http_scheme: https"
        )
    return settings, {
        "pentest_target_host": target_host,
        "pentest_ports": ports,
        "pentest_http_scheme": http_scheme,
        "pentest_http_port": http_port,
        "pentest_http_path": http_path,
        "pentest_connect_timeout": connect_timeout,
        "pentest_request_timeout": request_timeout,
        "pentest_verify_tls": verify_tls,
        "pentest_allow_public_target": allow_public_target,
        "pentest_rtsp_probe": rtsp_probe,
        "pentest_http_options_probe": http_options_probe,
        "pentest_tls_assessment": tls_assessment,
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


def _resolve_ptz(
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
    _reject_unknown(raw, PTZ_KEYS, context)
    enabled = _as_bool(raw.get("enabled", False), f"{context}.enabled")
    provider = str(raw.get("provider", "onvif" if enabled else "none")).strip().lower()
    if provider not in {"none", "onvif"}:
        raise ConfigurationError(f"{context}.provider must be none or onvif")
    port = _integer_value(raw.get("port", 2020), f"{context}.port")
    if not 1 <= port <= 65535:
        raise ConfigurationError(f"{context}.port must be between 1 and 65535")
    token = _optional_text(raw.get("profile_token"), f"{context}.profile_token")
    velocity = _number_value(raw.get("velocity", 0.35), f"{context}.velocity")
    duration = _number_value(raw.get("move_duration_seconds", 0.7), f"{context}.move_duration_seconds")
    if not 0 < velocity <= 1:
        raise ConfigurationError(f"{context}.velocity must be between 0 and 1")
    if not 0.1 <= duration <= 5:
        raise ConfigurationError(f"{context}.move_duration_seconds must be between 0.1 and 5")
    if enabled:
        if provider != "onvif":
            raise ConfigurationError(f"{context}.provider=onvif is required when enabled")
        if source_type != "rtsp" or settings.rtsp_url_override:
            raise ConfigurationError(f"{context}.provider=onvif requires structured RTSP source")
        if not (settings.rtsp_host and settings.rtsp_username and settings.rtsp_password):
            raise ConfigurationError(f"{context}.provider=onvif requires structured host and camera account")
    return {
        "ptz_enabled": enabled,
        "ptz_provider": provider,
        "ptz_port": port,
        "ptz_profile_token": token,
        "ptz_velocity": velocity,
        "ptz_move_duration_seconds": duration,
    }


def _resolve_auto_patrol(
    raw: Any,
    *,
    ptz_enabled: bool,
    rules: RulesConfig,
    context: str,
) -> dict[str, Any]:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigurationError(f"{context} must be an object")
    _reject_unknown(raw, AUTO_PATROL_KEYS, context)
    enabled = _as_bool(raw.get("enabled", False), f"{context}.enabled")
    observe = _number_value(raw.get("observe_seconds", 5.0), f"{context}.observe_seconds")
    interval = _number_value(
        raw.get("search_move_interval_seconds", 1.0), f"{context}.search_move_interval_seconds"
    )
    alarms = _integer_value(raw.get("max_alarm_events_per_screen", 3), f"{context}.max_alarm_events_per_screen")
    if not 1 <= observe <= 120:
        raise ConfigurationError(f"{context}.observe_seconds must be between 1 and 120")
    if not 0.1 <= interval <= 30:
        raise ConfigurationError(f"{context}.search_move_interval_seconds must be between 0.1 and 30")
    if not 1 <= alarms <= 10:
        raise ConfigurationError(f"{context}.max_alarm_events_per_screen must be between 1 and 10")
    if enabled and not ptz_enabled:
        raise ConfigurationError(f"{context}.enabled requires ptz.enabled: true")
    if enabled and rules.facebook.cooldown_seconds != 3:
        raise ConfigurationError(f"{context} requires facebook_usage.cooldown_seconds: 3")
    return {
        "auto_patrol_enabled": enabled,
        "auto_patrol_observe_seconds": observe,
        "auto_patrol_search_move_interval_seconds": interval,
        "auto_patrol_max_alarm_events_per_screen": alarms,
    }


def _resolve_features(raw: Any, *, context: str) -> dict[str, Any]:
    """Resolve the installed, exclusive feature catalog for one device.

    Model values are optional at manifest load time so an existing Facebook
    fleet can upgrade safely before the operator stages the verified person
    model.  Selecting Person Guard later fails closed until both references
    resolve and the SHA-256 matches.
    """

    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigurationError(f"{context} must be an object")
    _reject_unknown(raw, FEATURES_KEYS, context)
    raw_available = raw.get("available", [FeatureId.FACEBOOK_MONITOR.value])
    if not isinstance(raw_available, list) or not raw_available:
        raise ConfigurationError(f"{context}.available must be a non-empty list")
    allowed: set[FeatureId] = set()
    for index, value in enumerate(raw_available, start=1):
        if not isinstance(value, str):
            raise ConfigurationError(f"{context}.available[{index}] must be text")
        try:
            feature = FeatureId(value.strip())
        except ValueError as exc:
            raise ConfigurationError(f"{context}.available[{index}] is unsupported") from exc
        if feature == FeatureId.NONE:
            raise ConfigurationError(f"{context}.available cannot include none")
        if feature in allowed:
            raise ConfigurationError(f"{context}.available contains a duplicate feature")
        allowed.add(feature)
    default_raw = raw.get("default", FeatureId.FACEBOOK_MONITOR.value)
    if not isinstance(default_raw, str):
        raise ConfigurationError(f"{context}.default must be text")
    try:
        default = FeatureId(default_raw.strip())
    except ValueError as exc:
        raise ConfigurationError(f"{context}.default is unsupported") from exc
    if default not in allowed:
        raise ConfigurationError(f"{context}.default must be present in available")

    person_path: Path | None = None
    person_sha256: str | None = None
    person_config = PersonGuardConfig()
    raw_person = raw.get("person_guard")
    if FeatureId.PERSON_GUARD in allowed:
        if not isinstance(raw_person, dict):
            raise ConfigurationError(f"{context}.person_guard is required when person_guard is available")
        _reject_unknown(raw_person, PERSON_GUARD_KEYS, f"{context}.person_guard")
        path_env = _required_env_reference(
            raw_person.get("model_path_env"), f"{context}.person_guard.model_path_env"
        )
        sha_env = _required_env_reference(
            raw_person.get("model_sha256_env"), f"{context}.person_guard.model_sha256_env"
        )
        resolved_path = _env_value(
            path_env, required=False, context=f"{context}.person_guard.model_path_env"
        )
        person_path = Path(resolved_path).expanduser() if resolved_path else None
        person_sha256 = _env_value(
            sha_env, required=False, context=f"{context}.person_guard.model_sha256_env"
        )
        try:
            person_config = PersonGuardConfig(
                minimum_confidence=_number_value(
                    raw_person.get("minimum_confidence", 0.45),
                    f"{context}.person_guard.minimum_confidence",
                ),
                confirmation_frames=_integer_value(
                    raw_person.get("confirmation_frames", 2),
                    f"{context}.person_guard.confirmation_frames",
                ),
                absence_rearm_seconds=_number_value(
                    raw_person.get("absence_rearm_seconds", 3.0),
                    f"{context}.person_guard.absence_rearm_seconds",
                ),
                tracking_dead_zone=_number_value(
                    raw_person.get("tracking_dead_zone", 0.15),
                    f"{context}.person_guard.tracking_dead_zone",
                ),
                tracking_move_duration_seconds=_number_value(
                    raw_person.get("tracking_move_duration_seconds", 0.25),
                    f"{context}.person_guard.tracking_move_duration_seconds",
                ),
                tracking_move_interval_seconds=_number_value(
                    raw_person.get("tracking_move_interval_seconds", 0.5),
                    f"{context}.person_guard.tracking_move_interval_seconds",
                ),
                search_move_interval_seconds=_number_value(
                    raw_person.get("search_move_interval_seconds", 2.0),
                    f"{context}.person_guard.search_move_interval_seconds",
                ),
                search_move_duration_seconds=_number_value(
                    raw_person.get("search_move_duration_seconds", 0.5),
                    f"{context}.person_guard.search_move_duration_seconds",
                ),
            )
            person_config.validate()
        except ValueError as exc:
            raise ConfigurationError(f"{context}.person_guard: {exc}") from exc
    elif raw_person is not None:
        raise ConfigurationError(f"{context}.person_guard requires person_guard in available")
    return {
        "feature_allowed": frozenset(allowed),
        "feature_default": default,
        "person_model_path": person_path,
        "person_model_sha256": person_sha256,
        "person_guard_config": person_config,
    }


def _resolve_control(raw: Any, *, context: str) -> dict[str, Any]:
    """Read an optional hub-only authenticated preview endpoint."""

    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigurationError(f"{context} must be an object")
    _reject_unknown(raw, CONTROL_KEYS, context)
    preview_url_env = raw.get("preview_url_env")
    preview_token_env = raw.get("preview_token_env")
    if preview_url_env is None and preview_token_env is None:
        return {"control_preview_url": None, "control_preview_token": None}
    url_env = _required_env_reference(preview_url_env, f"{context}.preview_url_env")
    token_env = _required_env_reference(preview_token_env, f"{context}.preview_token_env")
    url = _env_value(url_env, required=False, context=f"{context}.preview_url_env")
    token = _env_value(token_env, required=False, context=f"{context}.preview_token_env")
    if url and not url.startswith("https://"):
        raise ConfigurationError(f"{context}.preview_url_env must resolve to an https URL")
    return {"control_preview_url": url, "control_preview_token": token}


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
