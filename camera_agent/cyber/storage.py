from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import threading
from collections.abc import Iterator

from .models import (
    CyberEvent,
    DiscoveredAsset,
    ServiceFingerprint,
    VulnerabilityFinding,
)


class CyberStorage:
    """Local metadata store.  It never accepts credentials or camera pixels."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS cyber_assets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ip TEXT NOT NULL,
                    mac TEXT,
                    vendor TEXT,
                    hostname TEXT,
                    discovery_method TEXT NOT NULL,
                    first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS cyber_services (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ip TEXT NOT NULL,
                    port INTEGER NOT NULL,
                    protocol TEXT NOT NULL,
                    service TEXT NOT NULL,
                    version TEXT,
                    banner TEXT,
                    tls INTEGER NOT NULL,
                    confidence REAL NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS cyber_findings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ip TEXT NOT NULL,
                    cve_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    cvss REAL NOT NULL,
                    severity TEXT NOT NULL,
                    source TEXT NOT NULL,
                    cisa_kev INTEGER NOT NULL,
                    confidence REAL NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS cyber_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    agent_id TEXT NOT NULL,
                    camera_id TEXT NOT NULL,
                    target_ip TEXT NOT NULL,
                    device_class TEXT NOT NULL,
                    service TEXT NOT NULL,
                    port INTEGER,
                    risk_score REAL,
                    risk_severity TEXT,
                    event_json TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS cyber_outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    target_ip TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
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

    def save_asset(self, asset: DiscoveredAsset) -> None:
        with self._lock, self._connection() as connection:
            connection.execute(
                "INSERT INTO cyber_assets(ip,mac,vendor,hostname,discovery_method,first_seen,last_seen) VALUES(?,?,?,?,?,?,?)",
                (asset.ip, asset.mac, asset.vendor, asset.hostname, asset.discovery_method, asset.first_seen, asset.last_seen),
            )

    def save_service(self, fingerprint: ServiceFingerprint) -> None:
        # Banner is already sanitized by the fingerprinter and remains bounded.
        with self._lock, self._connection() as connection:
            connection.execute(
                "INSERT INTO cyber_services(ip,port,protocol,service,version,banner,tls,confidence) VALUES(?,?,?,?,?,?,?,?)",
                (fingerprint.ip, fingerprint.port, fingerprint.protocol, fingerprint.service, fingerprint.version, fingerprint.banner, int(fingerprint.tls), fingerprint.confidence),
            )

    def save_finding(self, ip: str, finding: VulnerabilityFinding) -> None:
        with self._lock, self._connection() as connection:
            connection.execute(
                "INSERT INTO cyber_findings(ip,cve_id,title,cvss,severity,source,cisa_kev,confidence) VALUES(?,?,?,?,?,?,?,?)",
                (ip, finding.cve_id, finding.title, finding.cvss, finding.severity, finding.source, int(finding.cisa_kev), finding.confidence),
            )

    def save_event(self, event: CyberEvent, *, pending: bool = True) -> None:
        payload = json.dumps(event.to_dict(), ensure_ascii=True, separators=(",", ":"))
        risk = event.risk
        with self._lock, self._connection() as connection:
            connection.execute(
                "INSERT INTO cyber_events(agent_id,camera_id,target_ip,device_class,service,port,risk_score,risk_severity,event_json) VALUES(?,?,?,?,?,?,?,?,?)",
                (event.agent_id, event.camera_id, event.target_ip, event.device_class, event.service, event.port, risk.score if risk else None, risk.severity if risk else None, payload),
            )
            if pending:
                connection.execute(
                    "INSERT INTO cyber_outbox(target_ip,payload_json,status) VALUES(?,?,?)",
                    (event.target_ip, payload, "pending"),
                )

    @staticmethod
    def event_payload(event: CyberEvent) -> str:
        return json.dumps(event.to_dict(), ensure_ascii=True, separators=(",", ":"))

    def mark_outbox_sent(self, target_ip: str, payload: str) -> None:
        with self._lock, self._connection() as connection:
            connection.execute(
                "UPDATE cyber_outbox SET status='sent' WHERE id = (SELECT id FROM cyber_outbox WHERE target_ip=? AND payload_json=? AND status='pending' ORDER BY id LIMIT 1)",
                (target_ip, payload),
            )


__all__ = ["CyberStorage"]
