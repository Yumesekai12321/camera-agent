from __future__ import annotations

from dataclasses import dataclass, field, replace
import logging
from pathlib import Path
import sqlite3
import time

import requests

from .decision import AgentState, Decision
from .features import FEATURE_CODES, FeatureId
from .rules import RuleEvaluation
from .outbox import OutboxRecord, SQLiteEventOutbox


LOGGER = logging.getLogger(__name__)


class MainfluxError(RuntimeError):
    pass


@dataclass(frozen=True)
class Telemetry:
    decision: Decision | None
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
    active_feature: FeatureId = FeatureId.FACEBOOK_MONITOR
    runtime_enabled: bool = True
    person_present: bool = False
    person_confidence: float = 0.0
    person_tracking: bool = False
    feature_alert_event: bool = False
    feature_event_cleared: bool = False
    control_generation: int = 0
    control_applied_event: bool = False


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
        self.last_signature: tuple[object, ...] | None = None
        self.last_publish_time = 0.0
        self.next_retry_time = 0.0
        self._outbox_path = outbox_path
        self._device_id = device_id
        self._destination_id = destination_id
        self.outbox: SQLiteEventOutbox | None = None
        if enabled and outbox_path is not None:
            self._ensure_outbox()
        self._last_enqueued_event: tuple[int, ...] | None = None
        self._last_custom_enqueued_event: tuple[object, ...] | None = None
        self._pending_events = {
            "event_triggered": False,
            "event_cleared": False,
            "camera_offline_event": False,
            "camera_recovered_event": False,
            "feature_alert_event": False,
            "feature_event_cleared": False,
            "control_applied_event": False,
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
                self._pending_events[name]
                or (
                    getattr(telemetry.rule, name)
                    if hasattr(telemetry.rule, name)
                    else getattr(telemetry, name)
                )
            )
        if not any(self._pending_events.values()):
            return telemetry
        rule_values = {
            name: value
            for name, value in self._pending_events.items()
            if hasattr(telemetry.rule, name)
        }
        telemetry_values = {
            name: value
            for name, value in self._pending_events.items()
            if not hasattr(telemetry.rule, name)
        }
        return replace(
            telemetry,
            rule=replace(telemetry.rule, **rule_values),
            **telemetry_values,
        )

    def _payload(self, telemetry: Telemetry) -> list[dict[str, object]]:
        decision = telemetry.decision
        payload: list[dict[str, object]] = [
            {
                "n": "camera_online",
                "v": int(telemetry.camera_online),
                "u": "bool",
            },
            {"n": "active_feature", "v": FEATURE_CODES[telemetry.active_feature], "u": "code"},
            {"n": "runtime_enabled", "v": int(telemetry.runtime_enabled), "u": "bool"},
            {"n": "person_present", "v": int(telemetry.person_present), "u": "bool"},
            {"n": "person_confidence", "v": round(float(telemetry.person_confidence), 4), "u": "probability"},
            {"n": "person_tracking", "v": int(telemetry.person_tracking), "u": "bool"},
            {"n": "feature_alert_event", "v": int(telemetry.feature_alert_event), "u": "bool"},
            {"n": "feature_event_cleared", "v": int(telemetry.feature_event_cleared), "u": "bool"},
            {"n": "control_generation", "v": int(telemetry.control_generation), "u": "count"},
            {"n": "control_applied_event", "v": int(telemetry.control_applied_event), "u": "bool"},
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
            # Keep the historical field for existing dashboards while the new
            # field makes its now feature-neutral meaning explicit.
            {"n": "facebook_rule_status", "v": int(telemetry.rule.status), "u": "code"},
            {"n": "feature_rule_status", "v": int(telemetry.rule.status), "u": "code"},
            {
                "n": "rule_violation_event",
                "v": int(telemetry.rule.event_triggered or telemetry.feature_alert_event),
                "u": "bool",
            },
            {
                "n": "rule_event_cleared",
                "v": int(telemetry.rule.event_cleared or telemetry.feature_event_cleared),
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
        if decision is not None:
            payload[0:0] = [
                {"n": "agent_state", "v": int(decision.state), "u": "code"},
                {"n": "computer_detected", "v": int(decision.computer_detected), "u": "bool"},
                {"n": "facebook_active", "v": int(decision.facebook_active), "u": "bool"},
                {"n": "facebook_confidence", "v": round(decision.active_score, 4), "u": "probability"},
                {"n": "computer_confidence", "v": round(float(telemetry.computer_confidence), 4), "u": "probability"},
                {"n": "classifier_degraded", "v": int(telemetry.classifier_degraded), "u": "bool"},
                {"n": "facebook_raw_confidence", "v": round(float(telemetry.raw_active_score), 4), "u": "probability"},
                {"n": "classifier_margin", "v": round(float(telemetry.classifier_margin), 4), "u": "probability"},
            ]
        return payload

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
            int(telemetry.feature_alert_event),
            int(telemetry.feature_event_cleared),
            int(telemetry.control_applied_event),
            int(telemetry.rule.violation_count),
            int(telemetry.control_generation),
        )
        has_event = any(event_fingerprint[:7])
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
        state = int(telemetry.decision.state) if telemetry.decision is not None else None
        signature = (
            state,
            int(telemetry.rule.status),
            telemetry.camera_online,
            telemetry.active_feature.value,
            telemetry.runtime_enabled,
            telemetry.person_present,
            telemetry.control_generation,
        )
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
        state_label = AgentState(state).name if state is not None else telemetry.active_feature.value
        LOGGER.info("Mainflux accepted %s (HTTP %s)", state_label, response.status_code)
        if has_event:
            LOGGER.warning(
                "MAINFLUX ALARM PULSE delivered: feature=%s offline=%s cleared=%s",
                int(telemetry.rule.event_triggered or telemetry.feature_alert_event),
                int(telemetry.rule.camera_offline_event),
                int(telemetry.rule.event_cleared),
            )
        return True

    def publish_payload(
        self,
        payload: list[dict[str, object]],
        *,
        signature: tuple[object, ...],
        event: bool = False,
        force: bool = False,
    ) -> bool:
        """Publish a non-camera SenML payload using the same durable outbox.

        This is intentionally small and generic for bounded auxiliary agents
        such as the pentest worker.  Event payloads must enter SQLite before
        the HTTP request, just like camera rule pulses.
        """
        if not self.enabled:
            return False
        if not self.thing_key:
            raise MainfluxError("Mainflux is enabled but its Thing key is missing")

        outbox = self._ensure_outbox()
        pending_record: OutboxRecord | None = None
        if event:
            if outbox is None:
                raise MainfluxError(
                    "Mainflux edge event outbox is unavailable; event was not published"
                )
            try:
                if signature != self._last_custom_enqueued_event:
                    outbox.enqueue(payload)
                    self._last_custom_enqueued_event = signature
            except (OSError, sqlite3.Error, ValueError) as exc:
                raise MainfluxError(
                    "Mainflux edge event outbox is unavailable; event was not published"
                ) from exc
        if outbox is not None:
            try:
                pending_record = outbox.peek()
            except (OSError, sqlite3.Error, ValueError) as exc:
                if event:
                    raise MainfluxError(
                        "Mainflux edge event outbox is unavailable; event was not published"
                    ) from exc
                LOGGER.error("Mainflux event outbox unavailable: %s", exc)
            force = force or pending_record is not None

        now = self.clock()
        if now < self.next_retry_time:
            return False
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
                json=pending_record.payload if pending_record is not None else payload,
                timeout=self.timeout,
                verify=self.verify_tls,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            self.next_retry_time = now + self.retry_backoff
            raise MainfluxError(str(exc)) from exc

        self.last_signature = signature
        self.last_publish_time = now
        self.next_retry_time = 0.0
        if pending_record is not None and outbox is not None:
            try:
                outbox.delete(pending_record.record_id)
            except (OSError, sqlite3.Error) as exc:
                LOGGER.error("Published event but could not clear its outbox row: %s", exc)
        LOGGER.info("Mainflux accepted auxiliary telemetry (HTTP %s)", response.status_code)
        return True
