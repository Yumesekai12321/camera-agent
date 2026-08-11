from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import os
from typing import Any
from urllib.parse import quote

from camera_agent.camera import ADBCamera, RTSPCamera, WebcamCamera, WindowCamera
from camera_agent.config import redact_url
from camera_agent.device_registry import validate_device_spec


class DeviceCheckError(RuntimeError):
    pass


@dataclass(frozen=True)
class SourceCheckResult:
    device_id: str
    source_type: str
    checked: bool
    width: int | None = None
    height: int | None = None
    safe_source: str | None = None


def check_device(
    device: Mapping[str, Any],
    secrets: Mapping[str, str],
    *,
    timeout: float = 12.0,
    camera_factory: Callable[[Mapping[str, Any], Mapping[str, str]], Any] | None = None,
) -> SourceCheckResult:
    """Read one fresh frame without persisting pixels or publishing telemetry."""
    validate_device_spec(device)
    if timeout <= 0:
        raise DeviceCheckError("Source-check timeout must be greater than zero")
    if not device["enabled"]:
        return SourceCheckResult(device["device_id"], device["source_type"], False)

    factory = camera_factory or build_candidate_camera
    camera = factory(device, secrets)
    with camera:
        packet = camera.read_latest(timeout=timeout)
    if packet is None:
        detail = getattr(camera, "last_error", None) or "no fresh frame was received"
        raise DeviceCheckError(
            f"Source check failed for {device['device_id']} ({camera.safe_url}): {detail}"
        )
    height, width = packet.frame.shape[:2]
    if width < 1 or height < 1:
        raise DeviceCheckError(f"Source check returned an empty frame for {device['device_id']}")
    return SourceCheckResult(
        device["device_id"],
        device["source_type"],
        True,
        width=width,
        height=height,
        safe_source=camera.safe_url,
    )


def build_candidate_camera(
    device: Mapping[str, Any], secrets: Mapping[str, str]
):
    source_type = device["source_type"]
    source = device["source"]
    read_timeout = float(source.get("read_timeout", 8.0))
    reconnect_initial = float(source.get("reconnect_initial", 1.0))
    reconnect_max = float(source.get("reconnect_max", 15.0))
    if source_type == "rtsp":
        if source.get("url_env"):
            url = _resolve_reference(source["url_env"], secrets)
        else:
            credentials = ""
            if source.get("username_env"):
                username = _resolve_reference(source["username_env"], secrets)
                password = _resolve_reference(source["password_env"], secrets)
                credentials = f"{quote(username, safe='')}:{quote(password, safe='')}@"
            path = str(source["path"]).lstrip("/")
            url = (
                f"rtsp://{credentials}{source['host']}:{int(source.get('port', 554))}/"
                f"{path}"
            )
        return RTSPCamera(
            url,
            safe_url=redact_url(url),
            open_timeout=float(source.get("open_timeout", 8.0)),
            read_timeout=read_timeout,
            reconnect_initial=reconnect_initial,
            reconnect_max=reconnect_max,
        )
    if source_type == "adb":
        serial = None
        if source.get("serial_env"):
            serial = _resolve_reference(source["serial_env"], secrets)
        return ADBCamera(
            serial,
            frames_per_second=float(source.get("fps", 1.0)),
            command_timeout=float(source.get("command_timeout", 6.0)),
            require_landscape=bool(source.get("require_landscape", False)),
            minimum_frame_std=float(source.get("minimum_frame_std", 2.0)),
            reconnect_initial=reconnect_initial,
            reconnect_max=reconnect_max,
        )
    if source_type == "window":
        return WindowCamera(
            source["title"],
            capture_method=source.get("capture_method", "auto"),
            frames_per_second=float(source.get("fps", 8.0)),
            minimum_frame_std=float(source.get("minimum_frame_std", 2.0)),
            read_timeout=read_timeout,
            reconnect_initial=reconnect_initial,
            reconnect_max=reconnect_max,
        )
    return WebcamCamera(
        int(source.get("index", 0)),
        backend=source.get("backend", "dshow"),
        width=source.get("width"),
        height=source.get("height"),
        read_timeout=read_timeout,
        reconnect_initial=reconnect_initial,
        reconnect_max=reconnect_max,
    )


def _resolve_reference(reference: str, supplied: Mapping[str, str]) -> str:
    value = supplied.get(reference)
    if value is None:
        value = os.getenv(reference)
    if value is None or not value.strip():
        raise DeviceCheckError(
            f"Environment variable referenced by the candidate source is empty: {reference}"
        )
    return value.strip()
