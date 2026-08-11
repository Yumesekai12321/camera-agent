from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
from pathlib import Path
import os
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from dotenv import load_dotenv


PROJECT_DIR = Path(__file__).resolve().parent.parent


class ConfigurationError(ValueError):
    """Raised when a runtime setting is missing or invalid."""


def build_mainflux_destination_id(
    *,
    thing_id: str | None,
    thing_key: str | None,
    thing_key_env: str,
) -> str:
    """Build a stable non-secret outbox binding for the current destination."""
    if thing_id:
        return f"thing:{thing_id}"
    if thing_key:
        digest = hashlib.sha256(thing_key.encode("utf-8")).hexdigest()
        return f"key-sha256:{digest}"
    return f"env:{thing_key_env}"


def _text(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return value.strip()


def _boolean(name: str, default: bool) -> bool:
    value = _text(name)
    if value is None:
        return default
    normalized = value.lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{name} must be true or false, got {value!r}")


def _integer(name: str, default: int, minimum: int | None = None) -> int:
    raw = _text(name)
    try:
        value = int(raw) if raw is not None else default
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc
    if minimum is not None and value < minimum:
        raise ConfigurationError(f"{name} must be >= {minimum}")
    return value


def _number(name: str, default: float, minimum: float | None = None) -> float:
    raw = _text(name)
    try:
        value = float(raw) if raw is not None else default
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a number") from exc
    if minimum is not None and value < minimum:
        raise ConfigurationError(f"{name} must be >= {minimum}")
    return value


def parse_normalized_roi(value: str | None) -> tuple[float, float, float, float] | None:
    if not value:
        return None
    try:
        x, y, width, height = (float(part.strip()) for part in value.split(","))
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(
            "MONITOR_ROI must contain x,y,width,height as normalized numbers"
        ) from exc
    if min(x, y, width, height) < 0 or width <= 0 or height <= 0:
        raise ConfigurationError("MONITOR_ROI values must be positive")
    if x + width > 1 or y + height > 1:
        raise ConfigurationError("MONITOR_ROI must stay inside the frame (0..1)")
    return x, y, width, height


def redact_url(url: str) -> str:
    """Remove credentials before a URL is written to logs."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "<invalid-url>"
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    netloc = f"***:***@{host}" if parts.username is not None else host
    sensitive_query_names = {
        "access_token",
        "auth",
        "authorization",
        "key",
        "pass",
        "password",
        "secret",
        "token",
        "user",
        "username",
    }
    try:
        query = urlencode(
            [
                (name, "***" if name.lower() in sensitive_query_names else value)
                for name, value in parse_qsl(parts.query, keep_blank_values=True)
            ],
            doseq=True,
            safe="*",
        )
    except ValueError:
        query = "<redacted-query>"
    return urlunsplit((parts.scheme, netloc, parts.path, query, parts.fragment))


@dataclass(frozen=True)
class Settings:
    rtsp_adapter: str
    rtsp_host: str | None
    rtsp_username: str | None = field(repr=False)
    rtsp_password: str | None = field(repr=False)
    rtsp_path: str
    rtsp_port: int
    rtsp_url_override: str | None = field(repr=False)
    camera_open_timeout: float
    camera_read_timeout: float
    camera_offline_after: float
    reconnect_initial: float
    reconnect_max: float
    process_interval: float
    preview: bool
    preview_mode: str
    preview_max_width: int
    preview_max_height: int
    show_classifier_preview: bool
    monitor_roi: tuple[float, float, float, float] | None
    computer_model: Path
    classifier_model: Path
    computer_confidence: float
    detector_image_size: int
    torch_threads: int
    history_size: int
    minimum_history: int
    minimum_active_frames: int
    active_frame_threshold: float
    active_min_margin: float
    activate_average: float
    deactivate_average: float
    mainflux_enabled: bool
    mainflux_url: str
    mainflux_messages_path: str
    mainflux_thing_key: str | None = field(repr=False)
    mainflux_thing_id: str | None
    mainflux_destination_id: str
    mainflux_auth_scheme: str
    mainflux_heartbeat: float
    mainflux_timeout: float
    mainflux_retry_backoff: float
    mainflux_verify_tls: bool
    mainflux_outbox_path: Path
    save_evidence: bool
    evidence_dir: Path
    evidence_retention_days: int
    rules_file: Path

    @classmethod
    def from_env(cls, env_file: Path | None = None) -> "Settings":
        load_dotenv(env_file or PROJECT_DIR / ".env", override=False)
        key = _text("MAINFLUX_THING_KEY") or _text("ENDPOINT_KEY")
        preview_enabled = _boolean("SHOW_PREVIEW", True)
        preview_mode = (_text("PREVIEW_MODE", "compact") or "compact").lower()
        uses_generic_rtsp = any(
            _text(name) is not None
            for name in (
                "RTSP_ADAPTER",
                "RTSP_HOST",
                "RTSP_USERNAME",
                "RTSP_PASSWORD",
                "RTSP_PATH",
                "RTSP_PORT",
            )
        )
        rtsp_adapter = (
            _text("RTSP_ADAPTER", "generic" if uses_generic_rtsp else "tapo")
            or "generic"
        ).lower()
        rtsp_port_name = "RTSP_PORT" if _text("RTSP_PORT") is not None else "TAPO_PORT"
        mainflux_thing_id = _text("MAINFLUX_THING_ID")
        return cls(
            rtsp_adapter=rtsp_adapter,
            rtsp_host=_text("RTSP_HOST") or _text("TAPO_IP"),
            rtsp_username=_text("RTSP_USERNAME") or _text("TAPO_USER"),
            rtsp_password=_text("RTSP_PASSWORD") or _text("TAPO_PASS"),
            rtsp_path=(
                _text("RTSP_PATH") or _text("TAPO_STREAM", "stream1") or "stream1"
            ).lstrip("/"),
            rtsp_port=_integer(rtsp_port_name, 554, 1),
            rtsp_url_override=_text("RTSP_URL"),
            camera_open_timeout=_number("CAMERA_OPEN_TIMEOUT", 8.0, 0.1),
            camera_read_timeout=_number("CAMERA_READ_TIMEOUT", 8.0, 0.1),
            camera_offline_after=_number("CAMERA_OFFLINE_AFTER", 12.0, 1.0),
            reconnect_initial=_number("CAMERA_RECONNECT_INITIAL", 1.0, 0.1),
            reconnect_max=_number("CAMERA_RECONNECT_MAX", 15.0, 0.1),
            process_interval=_number("PROCESS_INTERVAL", 1.0, 0.05),
            preview=preview_enabled and preview_mode != "off",
            preview_mode=preview_mode,
            preview_max_width=_integer("PREVIEW_MAX_WIDTH", 1280, 320),
            preview_max_height=_integer("PREVIEW_MAX_HEIGHT", 720, 180),
            show_classifier_preview=_boolean("SHOW_CLASSIFIER_PREVIEW", False),
            monitor_roi=parse_normalized_roi(_text("MONITOR_ROI")),
            computer_model=Path(
                _text("COMPUTER_MODEL", str(PROJECT_DIR / "models" / "computer_detector.pt"))
                or ""
            ).expanduser(),
            classifier_model=Path(
                _text(
                    "FACEBOOK_MODEL",
                    str(PROJECT_DIR / "models" / "facebook_classifier.pt"),
                )
                or ""
            ).expanduser(),
            computer_confidence=_number("COMPUTER_CONFIDENCE", 0.35, 0.0),
            detector_image_size=_integer("DETECTOR_IMAGE_SIZE", 416, 64),
            torch_threads=_integer("TORCH_THREADS", 2, 1),
            history_size=_integer("HISTORY_SIZE", 8, 2),
            minimum_history=_integer("MINIMUM_HISTORY", 6, 1),
            minimum_active_frames=_integer("MINIMUM_ACTIVE_FRAMES", 5, 1),
            active_frame_threshold=_number("ACTIVE_FRAME_THRESHOLD", 0.55, 0.0),
            active_min_margin=_number("ACTIVE_MIN_MARGIN", 0.20, 0.0),
            activate_average=_number("ACTIVATE_AVERAGE", 0.72, 0.0),
            deactivate_average=_number("DEACTIVATE_AVERAGE", 0.35, 0.0),
            mainflux_enabled=_boolean("MAINFLUX_ENABLED", key is not None),
            mainflux_url=(_text("MAINFLUX_URL", "http://localhost") or "").rstrip("/"),
            mainflux_messages_path=_text("MAINFLUX_MESSAGES_PATH", "/http/messages")
            or "/http/messages",
            mainflux_thing_key=key,
            mainflux_thing_id=mainflux_thing_id,
            mainflux_destination_id=build_mainflux_destination_id(
                thing_id=mainflux_thing_id,
                thing_key=key,
                thing_key_env="MAINFLUX_THING_KEY",
            ),
            mainflux_auth_scheme=_text("MAINFLUX_AUTH_SCHEME", "Thing") or "",
            mainflux_heartbeat=_number("MAINFLUX_HEARTBEAT", 30.0, 1.0),
            mainflux_timeout=_number("MAINFLUX_TIMEOUT", 5.0, 0.1),
            mainflux_retry_backoff=_number("MAINFLUX_RETRY_BACKOFF", 10.0, 1.0),
            mainflux_verify_tls=_boolean("MAINFLUX_VERIFY_TLS", True),
            mainflux_outbox_path=Path(
                _text(
                    "MAINFLUX_OUTBOX_PATH",
                    str(PROJECT_DIR / "runtime" / "mainflux-outbox.sqlite3"),
                )
                or ""
            ).expanduser(),
            save_evidence=_boolean("SAVE_EVIDENCE", False),
            evidence_dir=Path(
                _text("EVIDENCE_DIR", str(PROJECT_DIR / "runtime" / "evidence")) or ""
            ).expanduser(),
            evidence_retention_days=_integer("EVIDENCE_RETENTION_DAYS", 7, 1),
            rules_file=Path(
                _text("RULES_FILE", str(PROJECT_DIR / "config" / "rules.toml")) or ""
            ).expanduser(),
        )

    @property
    def rtsp_url(self) -> str:
        if self.rtsp_url_override:
            return self.rtsp_url_override
        if not self.rtsp_host or not self.rtsp_path:
            raise ConfigurationError(
                "Set RTSP_HOST and RTSP_PATH (legacy TAPO_IP/TAPO_STREAM are also "
                "accepted), or provide RTSP_URL"
            )
        if bool(self.rtsp_username) != bool(self.rtsp_password):
            raise ConfigurationError(
                "RTSP_USERNAME and RTSP_PASSWORD must either both be set or both be empty"
            )
        credentials = ""
        if self.rtsp_username is not None and self.rtsp_password is not None:
            credentials = (
                f"{quote(self.rtsp_username, safe='')}:"
                f"{quote(self.rtsp_password, safe='')}@"
            )
        return f"rtsp://{credentials}{self.rtsp_host}:{self.rtsp_port}/{self.rtsp_path}"

    @property
    def redacted_rtsp_url(self) -> str:
        return redact_url(self.rtsp_url)

    @property
    def mainflux_messages_url(self) -> str:
        path = self.mainflux_messages_path
        if not path.startswith("/"):
            path = "/" + path
        return self.mainflux_url + path

    def validate(
        self,
        *,
        require_camera: bool = True,
        require_models: bool = True,
        require_mainflux: bool = True,
    ) -> None:
        errors: list[str] = []
        if require_camera:
            try:
                _ = self.rtsp_url
            except ConfigurationError as exc:
                errors.append(str(exc))
            if self.rtsp_adapter not in {"generic", "tapo"}:
                errors.append("RTSP_ADAPTER must be generic or tapo")
            if not 1 <= self.rtsp_port <= 65535:
                errors.append("RTSP_PORT must be between 1 and 65535")
            if (
                self.rtsp_adapter == "tapo"
                and self.rtsp_path not in {"stream1", "stream2"}
                and not self.rtsp_url_override
            ):
                errors.append("Tapo RTSP path must be stream1 or stream2")
        if require_models:
            if not self.computer_model.is_file():
                errors.append(f"Computer model not found: {self.computer_model}")
            if not self.classifier_model.is_file():
                errors.append(f"Facebook model not found: {self.classifier_model}")
        if self.minimum_history > self.history_size:
            errors.append("MINIMUM_HISTORY cannot exceed HISTORY_SIZE")
        if self.minimum_active_frames > self.history_size:
            errors.append("MINIMUM_ACTIVE_FRAMES cannot exceed HISTORY_SIZE")
        if self.deactivate_average >= self.activate_average:
            errors.append("DEACTIVATE_AVERAGE must be lower than ACTIVATE_AVERAGE")
        if self.preview_mode not in {"full", "compact", "off"}:
            errors.append("PREVIEW_MODE must be full, compact or off")
        for name, value in (
            ("COMPUTER_CONFIDENCE", self.computer_confidence),
            ("ACTIVE_FRAME_THRESHOLD", self.active_frame_threshold),
            ("ACTIVE_MIN_MARGIN", self.active_min_margin),
            ("ACTIVATE_AVERAGE", self.activate_average),
            ("DEACTIVATE_AVERAGE", self.deactivate_average),
        ):
            if not 0 <= value <= 1:
                errors.append(f"{name} must be between 0 and 1")
        if require_mainflux:
            if not self.mainflux_enabled:
                errors.append(
                    "Normal operation requires Mainflux: set MAINFLUX_ENABLED=true "
                    "or run agent.py --dry-run while calibrating"
                )
            elif not self.mainflux_thing_key:
                errors.append("MAINFLUX_ENABLED=true requires MAINFLUX_THING_KEY")
        if not self.rules_file.is_file():
            errors.append(f"Rules file not found: {self.rules_file}")
        if errors:
            raise ConfigurationError("\n- ".join(["Invalid configuration:", *errors]))
