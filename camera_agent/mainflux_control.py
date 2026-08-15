"""Durable, constrained Mainflux MQTT desired-state control plane.

Camera telemetry remains HTTP/SenML and never carries pixels.  This module is
only for a small allow-listed desired state.  The browser talks to a local
control hub with its own token; agents accept messages only from a Mainflux
topic that the operator restricts to the controller Thing and that device.
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
from typing import Callable, Iterable

from .control import CommandStore, ControlCommandError, validate_command
from .features import DesiredFeatureState, FeatureId


DEVICE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
SUBTOPIC_PREFIX_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
CHANNEL_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
MAX_MQTT_PAYLOAD_BYTES = 2048


class MainfluxControlError(ValueError):
    pass


def _required_text(environ: dict[str, str] | os._Environ[str], name: str) -> str:
    value = environ.get(name, "").strip()
    if not value:
        raise MainfluxControlError(f"{name} is required")
    return value


def _as_bool(value: str, name: str) -> bool:
    normalized = value.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise MainfluxControlError(f"{name} must be true or false")


@dataclass(frozen=True)
class MQTTControlSettings:
    host: str
    port: int
    tls: bool
    ca_file: Path | None
    subtopic_prefix: str

    @classmethod
    def from_environment(
        cls, environ: dict[str, str] | os._Environ[str] | None = None
    ) -> "MQTTControlSettings | None":
        source = os.environ if environ is None else environ
        host = source.get("MAINFLUX_MQTT_HOST", "").strip()
        if not host:
            return None
        try:
            port = int(source.get("MAINFLUX_MQTT_PORT", "8883"))
        except ValueError as exc:
            raise MainfluxControlError("MAINFLUX_MQTT_PORT must be an integer") from exc
        if not 1 <= port <= 65535:
            raise MainfluxControlError("MAINFLUX_MQTT_PORT must be 1..65535")
        tls = _as_bool(source.get("MAINFLUX_MQTT_TLS", "true"), "MAINFLUX_MQTT_TLS")
        ca_raw = source.get("MAINFLUX_MQTT_CA_FILE", "").strip()
        ca_file = Path(ca_raw).expanduser() if ca_raw else None
        if tls and ca_file is not None and not ca_file.is_file():
            raise MainfluxControlError("MAINFLUX_MQTT_CA_FILE does not exist")
        prefix = source.get("MAINFLUX_CONTROL_SUBTOPIC_PREFIX", "camera-agent").strip().strip("./")
        if not SUBTOPIC_PREFIX_PATTERN.fullmatch(prefix):
            raise MainfluxControlError("MAINFLUX_CONTROL_SUBTOPIC_PREFIX is invalid")
        return cls(host=host, port=port, tls=tls, ca_file=ca_file, subtopic_prefix=prefix)

    @staticmethod
    def _channel_id(channel_id: str) -> str:
        normalized = channel_id.strip()
        if not CHANNEL_ID_PATTERN.fullmatch(normalized):
            raise MainfluxControlError("control channel id is invalid")
        return normalized

    def _topic(self, channel_id: str, direction: str, device_id: str) -> str:
        channel = self._channel_id(channel_id)
        _validate_device_id(device_id)
        return f"channels/{channel}/messages/{self.subtopic_prefix}/{direction}/{device_id}"

    def desired_topic(self, channel_id: str, device_id: str) -> str:
        return self._topic(channel_id, "desired", device_id)

    def status_topic(self, channel_id: str, device_id: str) -> str:
        return self._topic(channel_id, "status", device_id)

    def command_topic(self, channel_id: str, device_id: str) -> str:
        return self._topic(channel_id, "command", device_id)


def _validate_device_id(device_id: str) -> str:
    normalized = device_id.strip()
    if not DEVICE_ID_PATTERN.fullmatch(normalized):
        raise MainfluxControlError("device_id is invalid")
    return normalized


def encode_desired_state(device_id: str, state: DesiredFeatureState) -> bytes:
    _validate_device_id(device_id)
    return json.dumps(
        {
            "schema": 1,
            "device_id": device_id,
            "generation": state.generation,
            "runtime_enabled": state.runtime_enabled,
            "feature": state.feature.value,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def decode_desired_state(payload: bytes | str) -> tuple[str, DesiredFeatureState]:
    raw = payload.encode("utf-8") if isinstance(payload, str) else payload
    if not 0 < len(raw) <= MAX_MQTT_PAYLOAD_BYTES:
        raise MainfluxControlError("control payload is empty or too large")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MainfluxControlError("control payload is invalid JSON") from exc
    if not isinstance(value, dict) or set(value) != {
        "schema", "device_id", "generation", "runtime_enabled", "feature"
    }:
        raise MainfluxControlError("control payload has unsupported fields")
    if value["schema"] != 1:
        raise MainfluxControlError("unsupported control schema")
    device_id = _validate_device_id(value["device_id"] if isinstance(value["device_id"], str) else "")
    generation = value["generation"]
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
        raise MainfluxControlError("control generation is invalid")
    if not isinstance(value["runtime_enabled"], bool):
        raise MainfluxControlError("runtime_enabled is invalid")
    try:
        feature = FeatureId(value["feature"])
    except (TypeError, ValueError) as exc:
        raise MainfluxControlError("feature is invalid") from exc
    return device_id, DesiredFeatureState(value["runtime_enabled"], feature, generation)


def encode_typed_command(device_id: str, action: str, payload: object) -> bytes:
    normalized = _validate_device_id(device_id)
    safe_payload = validate_command(normalized, action, payload)
    if action == "set_desired_state":
        raise MainfluxControlError("desired state must use the durable desired topic")
    return json.dumps(
        {"schema": 1, "device_id": normalized, "action": action, "payload": safe_payload},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def decode_typed_command(payload: bytes | str) -> tuple[str, str, dict[str, object]]:
    raw = payload.encode("utf-8") if isinstance(payload, str) else payload
    if not 0 < len(raw) <= MAX_MQTT_PAYLOAD_BYTES:
        raise MainfluxControlError("control payload is empty or too large")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MainfluxControlError("control payload is invalid JSON") from exc
    if not isinstance(value, dict) or set(value) != {"schema", "device_id", "action", "payload"}:
        raise MainfluxControlError("control payload has unsupported fields")
    if value["schema"] != 1:
        raise MainfluxControlError("unsupported control schema")
    device_id = _validate_device_id(value["device_id"] if isinstance(value["device_id"], str) else "")
    if not isinstance(value["action"], str):
        raise MainfluxControlError("command action is invalid")
    try:
        return device_id, value["action"], validate_command(device_id, value["action"], value["payload"])
    except ControlCommandError as exc:
        raise MainfluxControlError(str(exc)) from exc


@dataclass(frozen=True)
class DesiredStateRecord:
    device_id: str
    state: DesiredFeatureState
    updated_at: float
    applied_generation: int | None = None
    applied_status: str | None = None
    applied_detail: str | None = None
    applied_at: float | None = None


@dataclass(frozen=True)
class DevicePresence:
    device_id: str
    status: str
    last_seen_at: float
    detail: str | None = None


class DesiredStateStore:
    """The hub's durable source of truth and reconnect resend queue."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS desired_device_state (
                    device_id TEXT PRIMARY KEY,
                    runtime_enabled INTEGER NOT NULL,
                    feature TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    updated_at REAL NOT NULL,
                    applied_generation INTEGER,
                    applied_status TEXT,
                    applied_detail TEXT,
                    applied_at REAL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS desired_device_presence (
                    device_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    last_seen_at REAL NOT NULL,
                    detail TEXT
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> DesiredStateRecord:
        return DesiredStateRecord(
            device_id=row["device_id"],
            state=DesiredFeatureState(
                runtime_enabled=bool(row["runtime_enabled"]),
                feature=FeatureId(row["feature"]),
                generation=int(row["generation"]),
            ),
            updated_at=float(row["updated_at"]),
            applied_generation=(int(row["applied_generation"]) if row["applied_generation"] is not None else None),
            applied_status=row["applied_status"],
            applied_detail=row["applied_detail"],
            applied_at=(float(row["applied_at"]) if row["applied_at"] is not None else None),
        )

    def set_desired(
        self,
        device_id: str,
        *,
        runtime_enabled: bool,
        feature: FeatureId,
    ) -> DesiredStateRecord:
        normalized = _validate_device_id(device_id)
        if feature not in {FeatureId.FACEBOOK_MONITOR, FeatureId.PERSON_GUARD, FeatureId.NONE}:
            raise MainfluxControlError("feature is invalid")
        now = time.time()
        with self._lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT generation FROM desired_device_state WHERE device_id = ?", (normalized,)
            ).fetchone()
            generation = int(row["generation"]) + 1 if row is not None else 1
            connection.execute(
                """
                INSERT INTO desired_device_state(
                    device_id, runtime_enabled, feature, generation, updated_at,
                    applied_generation, applied_status, applied_detail, applied_at
                ) VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL, NULL)
                ON CONFLICT(device_id) DO UPDATE SET
                    runtime_enabled=excluded.runtime_enabled,
                    feature=excluded.feature,
                    generation=excluded.generation,
                    updated_at=excluded.updated_at,
                    applied_generation=NULL,
                    applied_status=NULL,
                    applied_detail=NULL,
                    applied_at=NULL
                """,
                (normalized, int(runtime_enabled), feature.value, generation, now),
            )
            connection.execute("COMMIT")
            return DesiredStateRecord(
                normalized,
                DesiredFeatureState(bool(runtime_enabled), feature, generation),
                now,
            )

    def get(self, device_id: str) -> DesiredStateRecord | None:
        normalized = _validate_device_id(device_id)
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM desired_device_state WHERE device_id = ?", (normalized,)
            ).fetchone()
        return None if row is None else self._row_to_record(row)

    def list(self) -> tuple[DesiredStateRecord, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM desired_device_state ORDER BY device_id"
            ).fetchall()
        return tuple(self._row_to_record(row) for row in rows)

    def acknowledge(
        self,
        device_id: str,
        state: DesiredFeatureState,
        *,
        status: str,
        detail: str | None = None,
    ) -> None:
        normalized = _validate_device_id(device_id)
        if status not in {"applied", "rejected", "online"}:
            raise MainfluxControlError("control acknowledgement status is invalid")
        # Presence is informational.  It must never overwrite the result of
        # the latest desired-state application in the browser UI.
        if status == "online":
            return
        safe_detail = (detail or "").replace("\r", " ").replace("\n", " ")[:160]
        now = time.time()
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT generation FROM desired_device_state WHERE device_id = ?", (normalized,)
            ).fetchone()
            if row is None or state.generation < int(row["generation"]):
                return
            # An acknowledgement must never move the hub's desired state
            # backward or overwrite a newer desired generation.
            if state.generation != int(row["generation"]):
                return
            connection.execute(
                """
                UPDATE desired_device_state
                SET applied_generation = ?, applied_status = ?, applied_detail = ?, applied_at = ?
                WHERE device_id = ?
                """,
                (state.generation, status, safe_detail or None, now, normalized),
            )

    def record_presence(
        self, device_id: str, *, status: str, detail: str | None = None
    ) -> None:
        """Record a safe agent ACK even before that device has desired state."""

        normalized = _validate_device_id(device_id)
        if status not in {"applied", "rejected", "online"}:
            raise MainfluxControlError("control acknowledgement status is invalid")
        safe_detail = (detail or "").replace("\r", " ").replace("\n", " ")[:160]
        with self._lock, closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT INTO desired_device_presence(device_id, status, last_seen_at, detail)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(device_id) DO UPDATE SET
                    status=excluded.status,
                    last_seen_at=excluded.last_seen_at,
                    detail=excluded.detail
                """,
                (normalized, status, time.time(), safe_detail or None),
            )

    def presence(self, device_id: str) -> DevicePresence | None:
        normalized = _validate_device_id(device_id)
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT device_id, status, last_seen_at, detail "
                "FROM desired_device_presence WHERE device_id = ?",
                (normalized,),
            ).fetchone()
        return None if row is None else DevicePresence(
            device_id=row["device_id"],
            status=row["status"],
            last_seen_at=float(row["last_seen_at"]),
            detail=row["detail"],
        )


class MQTTClient:
    """Small paho wrapper that never logs payloads or credentials."""

    def __init__(
        self,
        settings: MQTTControlSettings,
        *,
        client_id: str,
        thing_key: str,
        on_message: Callable[[str, bytes], None] | None = None,
        on_connected: Callable[[], None] | None = None,
    ) -> None:
        self.settings = settings
        self.client_id = _validate_device_id(client_id)
        self.thing_key = thing_key
        self.on_message = on_message
        self.on_connected = on_connected
        self._client = None
        self._connected = threading.Event()
        self._subscriptions: tuple[str, ...] = ()
        self._lock = threading.Lock()

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    def start(self, subscriptions: Iterable[str] = ()) -> None:
        self._subscriptions = tuple(subscriptions)
        if self._client is not None:
            return
        try:
            import paho.mqtt.client as mqtt
        except ImportError as exc:
            raise MainfluxControlError("Install paho-mqtt to enable Mainflux MQTT control") from exc
        try:
            callback_api = mqtt.CallbackAPIVersion.VERSION2
            client = mqtt.Client(callback_api, client_id=self.client_id, protocol=mqtt.MQTTv311)
        except (AttributeError, TypeError):  # pragma: no cover - paho 1.x compatibility
            client = mqtt.Client(client_id=self.client_id, protocol=mqtt.MQTTv311)
        # Mainflux MQTT authenticates a Thing with username=<thing_id> and
        # password=<thing_key>; MQTT client ID is not used as a credential.
        client.username_pw_set(self.client_id, self.thing_key)
        if self.settings.tls:
            client.tls_set(ca_certs=str(self.settings.ca_file) if self.settings.ca_file else None)
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        self._client = client
        client.connect_async(self.settings.host, self.settings.port, keepalive=30)
        client.loop_start()

    def stop(self) -> None:
        with self._lock:
            client, self._client = self._client, None
        self._connected.clear()
        if client is None:
            return
        try:
            client.disconnect()
        except Exception:
            pass
        client.loop_stop()

    def publish(self, topic: str, payload: bytes, *, qos: int = 1) -> bool:
        if not 0 < len(payload) <= MAX_MQTT_PAYLOAD_BYTES:
            raise MainfluxControlError("MQTT payload is empty or too large")
        client = self._client
        if client is None or not self.connected:
            return False
        result = client.publish(topic, payload=payload, qos=qos, retain=False)
        return getattr(result, "rc", 1) == 0

    def _on_connect(self, client, _userdata, _flags, reason_code, _properties=None) -> None:
        if int(reason_code) != 0:
            self._connected.clear()
            return
        for topic in self._subscriptions:
            client.subscribe(topic, qos=1)
        self._connected.set()
        if self.on_connected is not None:
            try:
                self.on_connected()
            except Exception:
                return

    def _on_disconnect(self, _client, _userdata, _disconnect_flags=None, _reason_code=None, _properties=None) -> None:
        self._connected.clear()

    def _on_message(self, _client, _userdata, message) -> None:
        if self.on_message is None:
            return
        payload = bytes(message.payload)
        if len(payload) > MAX_MQTT_PAYLOAD_BYTES:
            return
        try:
            self.on_message(str(message.topic), payload)
        except Exception:
            # Never echo control payloads; callers publish only a safe ACK.
            return


class MainfluxControlHub:
    """Hub-side state persistence, MQTT publish and acknowledgement updates."""

    def __init__(
        self,
        settings: MQTTControlSettings | None,
        store: DesiredStateStore,
        *,
        controller_thing_id: str | None = None,
        controller_thing_key: str | None = None,
        channel_by_device: dict[str, str] | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.channel_by_device = {
            _validate_device_id(device_id): MQTTControlSettings._channel_id(channel_id)
            for device_id, channel_id in (channel_by_device or {}).items()
        }
        self._mqtt: MQTTClient | None = None
        if settings is not None and controller_thing_id and controller_thing_key:
            self._mqtt = MQTTClient(
                settings,
                client_id=controller_thing_id,
                thing_key=controller_thing_key,
                on_message=self._on_status,
                on_connected=self.resend_all,
            )

    @property
    def ready(self) -> bool:
        return self._mqtt is not None and self._mqtt.connected

    @property
    def configured(self) -> bool:
        """Whether this hub has credentials for remote MQTT command delivery."""
        return self._mqtt is not None

    def start(self) -> None:
        if self._mqtt is None or self.settings is None:
            return
        self._mqtt.start(
            tuple(
                self.settings.status_topic(channel_id, device_id)
                for device_id, channel_id in self.channel_by_device.items()
            )
        )

    def stop(self) -> None:
        if self._mqtt is not None:
            self._mqtt.stop()

    def set_desired(
        self, device_id: str, *, runtime_enabled: bool, feature: FeatureId
    ) -> DesiredStateRecord:
        record = self.store.set_desired(
            device_id, runtime_enabled=runtime_enabled, feature=feature
        )
        self.publish(record)
        return record

    def publish(self, record: DesiredStateRecord) -> bool:
        if self._mqtt is None or self.settings is None:
            return False
        channel_id = self.channel_by_device.get(record.device_id)
        if channel_id is None:
            return False
        return self._mqtt.publish(
            self.settings.desired_topic(channel_id, record.device_id),
            encode_desired_state(record.device_id, record.state),
        )

    def publish_command(self, device_id: str, action: str, payload: object) -> bool:
        if self._mqtt is None or self.settings is None:
            return False
        channel_id = self.channel_by_device.get(device_id)
        if channel_id is None:
            return False
        return self._mqtt.publish(
            self.settings.command_topic(channel_id, device_id),
            encode_typed_command(device_id, action, payload),
        )

    def resend_all(self) -> int:
        return sum(1 for record in self.store.list() if self.publish(record))

    def _on_status(self, topic: str, payload: bytes) -> None:
        if self.settings is None or topic not in {
            self.settings.status_topic(channel_id, device_id)
            for device_id, channel_id in self.channel_by_device.items()
        }:
            return
        try:
            value = json.loads(payload.decode("utf-8"))
            if not isinstance(value, dict) or set(value) - {
                "schema", "device_id", "generation", "runtime_enabled", "feature", "status", "detail"
            }:
                return
            if value.get("schema") != 1 or not isinstance(value.get("status"), str):
                return
            device_id, state = decode_desired_state(
                json.dumps(
                    {
                        "schema": value["schema"],
                        "device_id": value["device_id"],
                        "generation": value["generation"],
                        "runtime_enabled": value["runtime_enabled"],
                        "feature": value["feature"],
                    }
                )
            )
            detail = value.get("detail") if isinstance(value.get("detail"), str) else None
            self.store.record_presence(
                device_id, status=value["status"], detail=detail
            )
            self.store.acknowledge(
                device_id,
                state,
                status=value["status"],
                detail=detail,
            )
        except (MainfluxControlError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
            return


class MainfluxAgentControl:
    """Agent-side MQTT bridge which only enqueues typed desired states."""

    def __init__(
        self,
        settings: MQTTControlSettings | None,
        *,
        device_id: str,
        thing_id: str | None,
        thing_key: str | None,
        control_channel_id: str | None,
        command_store: CommandStore,
        desired_store: DesiredStateStore | None = None,
    ) -> None:
        self.settings = settings
        self.device_id = _validate_device_id(device_id)
        self.command_store = command_store
        # In a local/no-MQTT deployment the loopback control server and agent
        # share this durable state store.  Keeping acknowledgements here lets
        # the UI distinguish an applied feature from merely a saved request.
        self.desired_store = desired_store or DesiredStateStore(
            command_store.path.with_name("control-desired-state.sqlite3")
        )
        self.control_channel_id = (
            MQTTControlSettings._channel_id(control_channel_id)
            if control_channel_id else None
        )
        self._mqtt: MQTTClient | None = None
        if settings is not None and thing_id and thing_key and self.control_channel_id:
            self._mqtt = MQTTClient(
                settings,
                client_id=thing_id,
                thing_key=thing_key,
                on_message=self._on_desired,
            )

    @property
    def configured(self) -> bool:
        return self._mqtt is not None

    def start(self) -> None:
        if self._mqtt is not None and self.settings is not None:
            self._mqtt.start((
                self.settings.desired_topic(self.control_channel_id, self.device_id),
                self.settings.command_topic(self.control_channel_id, self.device_id),
            ))
            return
        # The loopback deployment has no MQTT retained message.  Replay its
        # durable desired state into the same typed queue when the agent
        # restarts, otherwise FeatureRuntime would fall back to the manifest
        # default even though the UI says Person Guard was selected.
        record = self.desired_store.get(self.device_id)
        if record is not None:
            self._enqueue_desired(record.state)

    def stop(self) -> None:
        if self._mqtt is not None:
            self._mqtt.stop()

    def publish_ack(
        self,
        device_id: str,
        state: DesiredFeatureState,
        status: str,
        detail: str | None,
    ) -> None:
        if device_id != self.device_id or status not in {"applied", "rejected", "online"}:
            return
        self.desired_store.record_presence(device_id, status=status, detail=detail)
        self.desired_store.acknowledge(device_id, state, status=status, detail=detail)
        if self._mqtt is None or self.settings is None or self.control_channel_id is None:
            return
        # Details originate from typed validators; still cap and normalize it
        # before it crosses a process/network boundary.
        safe_detail = (detail or "").replace("\r", " ").replace("\n", " ")[:160]
        payload = json.dumps(
            {
                "schema": 1,
                "device_id": device_id,
                "generation": state.generation,
                "runtime_enabled": state.runtime_enabled,
                "feature": state.feature.value,
                "status": status,
                **({"detail": safe_detail} if safe_detail else {}),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        self._mqtt.publish(self.settings.status_topic(self.control_channel_id, device_id), payload)

    def _on_desired(self, topic: str, payload: bytes) -> None:
        if self.settings is None or self.control_channel_id is None:
            return
        if topic == self.settings.command_topic(self.control_channel_id, self.device_id):
            try:
                target, action, safe_payload = decode_typed_command(payload)
                if target == self.device_id:
                    self.command_store.enqueue(target, action, safe_payload)
            except (ControlCommandError, MainfluxControlError):
                return
            return
        if topic != self.settings.desired_topic(self.control_channel_id, self.device_id):
            return
        try:
            target, state = decode_desired_state(payload)
            if target != self.device_id:
                return
            self._enqueue_desired(state)
        except (ControlCommandError, MainfluxControlError):
            return

    def _enqueue_desired(self, state: DesiredFeatureState) -> None:
        self.command_store.enqueue(
            self.device_id,
            "set_desired_state",
            {
                "runtime_enabled": state.runtime_enabled,
                "feature": state.feature.value,
                "generation": state.generation,
            },
        )
