from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import threading
from typing import Any, Iterator


@dataclass(frozen=True)
class OutboxRecord:
    record_id: int
    payload: list[dict[str, Any]]


class SQLiteEventOutbox:
    """Small durable queue for edge events; it never stores credentials or frames."""

    def __init__(
        self,
        path: Path,
        device_id: str,
        destination_id: str = "",
    ) -> None:
        self.path = path
        self.device_id = device_id
        self.destination_id = destination_id
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS mainflux_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    device_id TEXT NOT NULL,
                    destination_id TEXT NOT NULL DEFAULT '',
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(mainflux_events)")
            }
            if "destination_id" not in columns:
                connection.execute(
                    "ALTER TABLE mainflux_events "
                    "ADD COLUMN destination_id TEXT NOT NULL DEFAULT ''"
                )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_mainflux_events_device
                ON mainflux_events(device_id, id)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_mainflux_events_destination
                ON mainflux_events(device_id, destination_id, id)
                """
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=5)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def enqueue(self, payload: list[dict[str, Any]]) -> None:
        encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=True)
        with self._lock, self._connection() as connection:
            connection.execute(
                """
                INSERT INTO mainflux_events(device_id, destination_id, payload_json)
                VALUES (?, ?, ?)
                """,
                (self.device_id, self.destination_id, encoded),
            )

    def peek(self) -> OutboxRecord | None:
        with self._lock, self._connection() as connection:
            row = connection.execute(
                """
                SELECT id, payload_json
                FROM mainflux_events
                WHERE device_id = ? AND destination_id = ?
                ORDER BY id
                LIMIT 1
                """,
                (self.device_id, self.destination_id),
            ).fetchone()
        if row is None:
            return None
        return OutboxRecord(int(row[0]), json.loads(str(row[1])))

    def delete(self, record_id: int) -> None:
        with self._lock, self._connection() as connection:
            connection.execute(
                """
                DELETE FROM mainflux_events
                WHERE id = ? AND device_id = ? AND destination_id = ?
                """,
                (record_id, self.device_id, self.destination_id),
            )
