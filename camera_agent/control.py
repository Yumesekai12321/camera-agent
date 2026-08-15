"""Durable local command queue shared by the PC control server and agents."""

from __future__ import annotations

from dataclasses import dataclass
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import threading
import time
import uuid
from typing import Any

from .features import FeatureId


class ControlCommandError(ValueError):
    pass


_DIRECTIONS = {"left", "right", "up", "down"}
_ACTIONS = {"move", "stop", "set_auto", "set_desired_state"}


@dataclass(frozen=True)
class ControlCommand:
    command_id: str
    device_id: str
    action: str
    payload: dict[str, object]
    created_at: float


@dataclass(frozen=True)
class CommandStatus:
    command_id: str
    device_id: str
    action: str
    status: str
    detail: str | None


def validate_command(device_id: str, action: str, payload: Any) -> dict[str, object]:
    if not isinstance(device_id, str) or not device_id:
        raise ControlCommandError("device_id is required")
    if action not in _ACTIONS:
        raise ControlCommandError("unsupported command action")
    if not isinstance(payload, dict):
        raise ControlCommandError("command payload must be an object")
    if action == "move":
        direction = payload.get("direction")
        if direction not in _DIRECTIONS:
            raise ControlCommandError("move command requires a safe direction")
        duration = payload.get("duration_seconds")
        if duration is not None and (
            isinstance(duration, bool) or not isinstance(duration, (int, float)) or not 0.1 <= duration <= 5
        ):
            raise ControlCommandError("move duration_seconds must be between 0.1 and 5")
        return {key: payload[key] for key in ("direction", "duration_seconds") if key in payload}
    if action == "set_auto":
        if not isinstance(payload.get("enabled"), bool):
            raise ControlCommandError("set_auto command requires boolean enabled")
        return {"enabled": payload["enabled"]}
    if action == "set_desired_state":
        expected = {"runtime_enabled", "feature", "generation"}
        if set(payload) != expected:
            raise ControlCommandError("set_desired_state requires runtime_enabled, feature and generation")
        if not isinstance(payload["runtime_enabled"], bool):
            raise ControlCommandError("runtime_enabled must be boolean")
        try:
            feature = FeatureId(str(payload["feature"]))
        except ValueError as exc:
            raise ControlCommandError("unsupported desired feature") from exc
        generation = payload["generation"]
        if isinstance(generation, bool) or not isinstance(generation, int) or not 0 <= generation <= 2**63 - 1:
            raise ControlCommandError("control generation must be a non-negative integer")
        return {
            "runtime_enabled": payload["runtime_enabled"],
            "feature": feature.value,
            "generation": generation,
        }
    if payload:
        raise ControlCommandError("stop command does not accept a payload")
    return {}


class CommandStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS control_commands (
                    command_id TEXT PRIMARY KEY,
                    device_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    status TEXT NOT NULL,
                    detail TEXT
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_control_commands_pending "
                "ON control_commands(device_id, status, created_at)"
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        return connection

    def enqueue(self, device_id: str, action: str, payload: Any) -> str:
        safe_payload = validate_command(device_id, action, payload)
        command_id = str(uuid.uuid4())
        with self._lock, closing(self._connect()) as connection:
            connection.execute(
                "INSERT INTO control_commands(command_id, device_id, action, payload_json, created_at, status) "
                "VALUES (?, ?, ?, ?, ?, 'pending')",
                (command_id, device_id, action, json.dumps(safe_payload, separators=(",", ":")), time.time()),
            )
        return command_id

    def claim(
        self,
        device_id: str,
        *,
        limit: int = 8,
        max_age_seconds: float = 120.0,
    ) -> tuple[ControlCommand, ...]:
        if max_age_seconds <= 0:
            raise ControlCommandError("max_age_seconds must be positive")
        cutoff = time.time() - max_age_seconds
        with self._lock, closing(self._connect()) as connection:
            # A stopped agent must not execute an old move unexpectedly when it
            # comes back online.  Commands are durable, but only briefly valid
            # for physical motion; callers can still inspect the rejected row.
            connection.execute(
                "UPDATE control_commands SET status = 'rejected', detail = 'expired' "
                "WHERE device_id = ? AND status = 'pending' AND created_at < ?",
                (device_id, cutoff),
            )
            rows = connection.execute(
                "SELECT command_id, device_id, action, payload_json, created_at "
                "FROM control_commands WHERE device_id = ? AND status = 'pending' "
                "ORDER BY created_at LIMIT ?",
                (device_id, limit),
            ).fetchall()
            if not rows:
                return ()
            connection.executemany(
                "UPDATE control_commands SET status = 'claimed' WHERE command_id = ? AND status = 'pending'",
                [(row["command_id"],) for row in rows],
            )
        return tuple(
            ControlCommand(
                command_id=row["command_id"],
                device_id=row["device_id"],
                action=row["action"],
                payload=json.loads(row["payload_json"]),
                created_at=float(row["created_at"]),
            )
            for row in rows
        )

    def complete(self, command_id: str, status: str, detail: str | None = None) -> None:
        if status not in {"executed", "rejected", "failed"}:
            raise ControlCommandError("invalid command completion status")
        with self._lock, closing(self._connect()) as connection:
            connection.execute(
                "UPDATE control_commands SET status = ?, detail = ? WHERE command_id = ?",
                (status, (detail or "")[:256], command_id),
            )

    def get(self, command_id: str) -> CommandStatus | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT command_id, device_id, action, status, detail FROM control_commands WHERE command_id = ?",
                (command_id,),
            ).fetchone()
        return None if row is None else CommandStatus(
            command_id=row["command_id"], device_id=row["device_id"], action=row["action"],
            status=row["status"], detail=row["detail"]
        )
