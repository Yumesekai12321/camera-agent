from __future__ import annotations

from dataclasses import dataclass
import logging
from pathlib import Path

from ..config import build_mainflux_destination_id
from ..mainflux import MainfluxError, MainfluxPublisher
from .config import CyberConfig
from .logging_utils import log_event
from .models import CyberEvent


LOGGER = logging.getLogger(__name__)


def _senml(name: str, value: object, unit: str) -> dict[str, object]:
    return {"n": name, "v": value, "u": unit}


def event_to_senml(event: CyberEvent) -> list[dict[str, object]]:
    risk = event.risk
    findings = [event.vulnerability] if event.vulnerability is not None else []
    validation_findings = sum(result.status == "FINDING" for result in event.validation)
    payload = [
        _senml("cyber_event", 1, "bool"),
        _senml("cyber_target_ip", event.target_ip, "text"),
        _senml("cyber_device_class", event.device_class, "text"),
        _senml("cyber_service", event.service or "unknown", "text"),
        _senml("cyber_port", event.port or 0, "count"),
        _senml("cyber_finding_count", len(findings), "count"),
        _senml("cyber_validation_finding_count", validation_findings, "count"),
        _senml("cyber_validated", int(bool(event.validation)), "bool"),
        _senml("cyber_correlation_status", event.correlation.status if event.correlation else "UNCONFIRMED", "text"),
        _senml("cyber_correlation_confidence", event.correlation.confidence if event.correlation else 0.0, "probability"),
        _senml("cyber_risk_score", risk.score if risk else 0.0, "score"),
        _senml("cyber_risk_severity", risk.severity if risk else "unknown", "text"),
    ]
    if event.vulnerability is not None:
        payload.extend(
            [
                _senml("cyber_cve", event.vulnerability.cve_id, "text"),
                _senml("cyber_cvss", event.vulnerability.cvss, "score"),
                _senml("cyber_cisa_kev", int(event.vulnerability.cisa_kev), "bool"),
            ]
        )
    return payload


@dataclass
class CyberMainfluxClient:
    """SenML publisher that reuses the project's durable Mainflux outbox."""

    config: CyberConfig
    dry_run: bool = False
    publisher: MainfluxPublisher | None = None
    logger: logging.Logger = LOGGER

    def __post_init__(self) -> None:
        values = self.config.resolved_mainflux_env()
        token = values.get("token")
        thing_id = values.get("thing_id")
        channel_id = values.get("channel_id")
        path = (
            f"/http/channels/{channel_id}/messages"
            if channel_id
            else self.config.mainflux.messages_path
        )
        adapter = self.config.mainflux.http_adapter_url or self.config.mainflux.base_url
        url = f"{adapter.rstrip('/')}/{path.lstrip('/')}"
        destination = build_mainflux_destination_id(
            thing_id=thing_id,
            thing_key=token,
            thing_key_env=self.config.mainflux.token_env,
        )
        if self.publisher is None:
            self.publisher = MainfluxPublisher(
                enabled=bool(self.config.mainflux.enabled and not self.dry_run),
                url=url,
                thing_key=token,
                auth_scheme="Thing",
                timeout=self.config.mainflux.timeout_seconds,
                retry_backoff=self.config.mainflux.retry_backoff_seconds,
                outbox_path=self.config.logging.file.parent / "cyber-outbox.sqlite3",
                device_id=thing_id or "cyber-agent",
                destination_id=destination,
            )
        self._token_present = bool(token)
        self.url = url
        self.destination_id = destination

    @property
    def enabled(self) -> bool:
        return bool(self.publisher and self.publisher.enabled)

    def publish(self, event: CyberEvent) -> bool:
        if not self.config.mainflux.enabled or self.dry_run:
            return False
        if not self._token_present:
            raise MainfluxError(f"Mainflux token env {self.config.mainflux.token_env} is missing")
        assert self.publisher is not None
        payload = event_to_senml(event)
        signature = (
            event.target_ip,
            event.service,
            event.port,
            event.vulnerability.cve_id if event.vulnerability else "",
            event.risk.score if event.risk else 0.0,
        )
        result = self.publisher.publish_payload(payload, signature=signature, event=True, force=True)
        if result:
            log_event(self.logger, "mainflux_publish_success", target_ip=event.target_ip)
        return result


__all__ = ["CyberMainfluxClient", "event_to_senml"]
