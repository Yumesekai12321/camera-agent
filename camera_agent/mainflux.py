from __future__ import annotations

from dataclasses import dataclass, field, replace
import logging
from pathlib import Path
import sqlite3
import time

import requests

from .decision import AgentState, Decision
from .rules import RuleEvaluation
from .outbox import OutboxRecord, SQLiteEventOutbox


LOGGER = logging.getLogger(__name__)


class MainfluxError(RuntimeError):
    pass


@dataclass(frozen=True)
class Telemetry:
    decision: Decision
    camera_online: bool
    computer_confidence: float = 0.0
    classifier_degraded: bool = False
    raw_active_score: float = 0.0
    classifier_margin: float = 0.0
    frame_age_seconds: float = 0.0
    inference_ms: float = 0.0
    rtsp_reconnect_count: int = 0
    self_preview_suppressed: bool = False
    rule: RuleEvaluation = field(default_factory=RuleEvaluation)


class MainfluxPublisher:
    def __init__(
        self,
        *,
        enabled: bool,
        url: str,
        thing_key: str | None,
        auth_scheme: str = "Thing",
        heartbeat_seconds: float = 30.0,
        timeout: float = 5.0,
        retry_backoff: float = 10.0,
        verify_tls: bool = True,
        session: requests.Session | None = None,
        clock=time.monotonic,
        outbox_path: Path | None = None,
        device_id: str = "default",
        destination_id: str = "legacy",
    ) -> None:
        self.enabled = enabled
        self.url = url
        self.thing_key = thing_key
        self.auth_scheme = auth_scheme.strip()
        self.heartbeat_seconds = heartbeat_seconds
        self.timeout = timeout
        self.retry_backoff = retry_backoff
        self.verify_tls = verify_tls
        self.session = session or requests.Session()
        self.clock = clock
        self.last_state: int | None = None
        self.last_signature: tuple[int, int, bool] | None = None
        self.last_publish_time = 0.0
        self.next_retry_time = 0.0
        self._outbox_path = outbox_path
        self._device_id = device_id
        self._destination_id = destination_id
        self.outbox: SQLiteEventOutbox | None = None
        if enabled and outbox_path is not None:
            self._ensure_outbox()
        self._last_enqueued_event: tuple[int, int, int, int, int] | None = None
        self._pending_events = {
            "event_triggered": False,
            "event_cleared": False,
            "camera_offline_event": False,
            "camera_recovered_event": False,
        }

    def _ensure_outbox(self) -> SQLiteEventOutbox | None:
        if self.outbox is not None:
            return self.outbox
        if self._outbox_path is None:
            return None
        try:
            self.outbox = SQLiteEventOutbox(
                self._outbox_path,
                self._device_id,
                self._destination_id,
            )
        except (OSError, sqlite3.Error) as exc:
            LOGGER.error("Could not initialize Mainflux event outbox: %s", exc)
        return self.outbox

    def _latch_events(self, telemetry: Telemetry) -> Telemetry:
        for name in self._pending_events:
            self._pending_events[name] = bool(
                self._pending_events[name] or getattr(telemetry.rule, name)
            )
        if not any(self._pending_events.values()):
            return telemetry
        return replace(
            telemetry,
            rule=replace(telemetry.rule, **self._pending_events),
        )

    def _payload(self, telemetry: Telemetry) -> list[dict[str, object]]:
        decision = telemetry.decision
        return [
            {"n": "agent_state", "v": int(decision.state), "u": "code"},
            {
                "n": "camera_online",
                "v": int(telemetry.camera_online),
                "u": "bool",
            },
            {
                "n": "computer_detected",
                "v": int(decision.computer_detected),
                "u": "bool",
            },
            {
                "n": "facebook_active",
                "v": int(decision.facebook_active),
                "u": "bool",
            },
            {
                "n": "facebook_confidence",
                "v": round(decision.active_score, 4),
                "u": "probability",
            },
            {
                "n": "computer_confidence",
                "v": round(float(telemetry.computer_confidence), 4),
                "u": "probability",
            },
            {
                "n": "classifier_degraded",
                "v": int(telemetry.classifier_degraded),
                "u": "bool",
            },
            {
                "n": "facebook_raw_confidence",
                "v": round(float(telemetry.raw_active_score), 4),
                "u": "probability",
            },
            {
                "n": "classifier_margin",
                "v": round(float(telemetry.classifier_margin), 4),
                "u": "probability",
            },
            {
                "n": "frame_age_seconds",
                "v": round(float(telemetry.frame_age_seconds), 3),
                "u": "s",
            },
            {
                "n": "inference_ms",
                "v": round(float(telemetry.inference_ms), 2),
                "u": "ms",
            },
            {
                "n": "rtsp_reconnect_count",
                "v": int(telemetry.rtsp_reconnect_count),
                "u": "count",
            },
            {
                "n": "self_preview_suppressed",
                "v": int(telemetry.self_preview_suppressed),
                "u": "bool",
            },
            {
                "n": "facebook_rule_status",
                "v": int(telemetry.rule.status),
                "u": "code",
            },
            {
                "n": "rule_violation_event",
                "v": int(telemetry.rule.event_triggered),
                "u": "bool",
            },
            {
                "n": "rule_event_cleared",
                "v": int(telemetry.rule.event_cleared),
                "u": "bool",
            },
            {
                "n": "rule_active_seconds",
                "v": round(float(telemetry.rule.active_seconds), 2),
                "u": "s",
            },
            {
                "n": "rule_violation_count",
                "v": int(telemetry.rule.violation_count),
                "u": "count",
            },
            {
                "n": "camera_offline_event",
                "v": int(telemetry.rule.camera_offline_event),
                "u": "bool",
            },
            {
                "n": "camera_recovered_event",
                "v": int(telemetry.rule.camera_recovered_event),
                "u": "bool",
            },
            {
                "n": "camera_offline_seconds",
                "v": round(float(telemetry.rule.camera_offline_seconds), 2),
                "u": "s",
            },
        ]

    def publish(self, telemetry: Telemetry, *, force: bool = False) -> bool:
        if not self.enabled:
            return False
        if not self.thing_key:
            raise MainfluxError("Mainflux is enabled but its Thing key is missing")

        telemetry = self._latch_events(telemetry)
        event_fingerprint = (
            int(telemetry.rule.event_triggered),
            int(telemetry.rule.event_cleared),
            int(telemetry.rule.camera_offline_event),
            int(telemetry.rule.camera_recovered_event),
            int(telemetry.rule.violation_count),
        )
        has_event = any(event_fingerprint[:4])
        pending_record: OutboxRecord | None = None
        outbox = self._ensure_outbox()
        if has_event and outbox is None:
            raise MainfluxError(
                "Mainflux edge event outbox is unavailable; event was not published"
            )
        if outbox is not None:
            try:
                if has_event and event_fingerprint != self._last_enqueued_event:
                    outbox.enqueue(self._payload(telemetry))
                    self._last_enqueued_event = event_fingerprint
                    for name in self._pending_events:
                        self._pending_events[name] = False
                pending_record = outbox.peek()
            except (OSError, sqlite3.Error, ValueError) as exc:
                if has_event:
                    raise MainfluxError(
                        "Mainflux edge event outbox is unavailable; event was not published"
                    ) from exc
                LOGGER.error("Mainflux event outbox unavailable: %s", exc)
            force = force or pending_record is not None
        now = self.clock()
        if now < self.next_retry_time:
            return False
        state = int(telemetry.decision.state)
        signature = (state, int(telemetry.rule.status), telemetry.camera_online)
        if not (
            force
            or self.last_signature != signature
            or now - self.last_publish_time >= self.heartbeat_seconds
        ):
            return False

        authorization = " ".join(
            part for part in (self.auth_scheme, self.thing_key) if part
        )
        try:
            response = self.session.post(
                self.url,
                headers={
                    "Authorization": authorization,
                    "Content-Type": "application/senml+json",
                },
                json=(
                    pending_record.payload
                    if pending_record is not None
                    else self._payload(telemetry)
                ),
                timeout=self.timeout,
                verify=self.verify_tls,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            self.next_retry_time = now + self.retry_backoff
            raise MainfluxError(str(exc)) from exc

        self.last_state = state
        self.last_signature = signature
        self.last_publish_time = now
        self.next_retry_time = 0.0
        if pending_record is not None and outbox is not None:
            try:
                outbox.delete(pending_record.record_id)
            except (OSError, sqlite3.Error) as exc:
                LOGGER.error("Published event but could not clear its outbox row: %s", exc)
        for name in self._pending_events:
            self._pending_events[name] = False
        LOGGER.info("Mainflux accepted %s (HTTP %s)", AgentState(state).name, response.status_code)
        if has_event:
            LOGGER.warning(
                "MAINFLUX ALARM PULSE delivered: facebook=%s offline=%s cleared=%s",
                int(telemetry.rule.event_triggered),
                int(telemetry.rule.camera_offline_event),
                int(telemetry.rule.event_cleared),
            )
        return True
