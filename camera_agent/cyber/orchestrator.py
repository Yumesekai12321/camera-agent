from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import logging
from collections.abc import Callable

from .config import CyberConfig
from .correlator import AssetCorrelator
from .discovery import NetworkDiscovery
from .fingerprint import ServiceFingerprinter
from .logging_utils import log_event
from .mainflux_client import CyberMainfluxClient
from .models import (
    CameraEvent,
    CorrelationResult,
    CyberEvent,
    DiscoveredAsset,
    PipelineReport,
    RiskAssessment,
    ServiceFingerprint,
    ValidationResult,
    VulnerabilityFinding,
)
from .risk_engine import RiskEngine
from .safe_validator import SafeValidator
from .scope_guard import ScopeGuard
from .storage import CyberStorage
from .vuln_matcher import VulnerabilityMatcher


LOGGER = logging.getLogger(__name__)


class CyberState(str, Enum):
    IDLE = "IDLE"
    DISCOVERING = "DISCOVERING"
    CORRELATING = "CORRELATING"
    FINGERPRINTING = "FINGERPRINTING"
    MATCHING_VULNS = "MATCHING_VULNS"
    VALIDATING = "VALIDATING"
    SCORING = "SCORING"
    PUBLISHING = "PUBLISHING"
    DONE = "DONE"
    ERROR = "ERROR"


@dataclass
class CyberOrchestrator:
    config: CyberConfig
    scope_guard: ScopeGuard
    discovery: NetworkDiscovery
    fingerprinter: ServiceFingerprinter
    matcher: VulnerabilityMatcher
    validator: SafeValidator
    risk_engine: RiskEngine
    correlator: AssetCorrelator
    mainflux: CyberMainfluxClient | None = None
    storage: CyberStorage | None = None
    logger: logging.Logger = LOGGER
    dry_run_override: bool | None = None
    state: CyberState = CyberState.IDLE
    state_history: list[str] = field(default_factory=lambda: [CyberState.IDLE.value])

    @classmethod
    def from_config(cls, config: CyberConfig, *, dry_run: bool | None = None) -> "CyberOrchestrator":
        guard = ScopeGuard(config.scope)
        storage = CyberStorage(config.logging.file.parent / "cyber.sqlite3")
        effective_dry_run = config.dry_run if dry_run is None else dry_run
        return cls(
            config=config,
            scope_guard=guard,
            discovery=NetworkDiscovery(config.discovery, guard),
            fingerprinter=ServiceFingerprinter(config.fingerprint, guard),
            matcher=VulnerabilityMatcher(config.vulnerability),
            validator=SafeValidator(config.validation, guard),
            risk_engine=RiskEngine(config.risk),
            correlator=AssetCorrelator(),
            mainflux=CyberMainfluxClient(config, dry_run=effective_dry_run),
            storage=storage,
            dry_run_override=effective_dry_run,
        )

    def _set_state(self, state: CyberState) -> None:
        self.state = state
        self.state_history.append(state.value)
        log_event(self.logger, "cyber_state_changed", state=state.value)

    def _mock_fingerprint(self, ip: str) -> ServiceFingerprint:
        return ServiceFingerprint(ip, 80, "http", "http", "mock", "Server: safe-mock", False, 0.5)

    def _select_correlations(
        self,
        camera_event: CameraEvent,
        assets: list[DiscoveredAsset],
        fingerprints: list[ServiceFingerprint],
    ) -> list[CorrelationResult]:
        return self.correlator.correlate(
            camera_event.device_class,
            assets,
            fingerprints,
            observed_at=camera_event.timestamp,
        )

    def process(self, camera_event: CameraEvent) -> PipelineReport:
        self.state_history = [CyberState.IDLE.value]
        self.state = CyberState.IDLE
        errors: list[str] = []
        assets: list[DiscoveredAsset] = []
        fingerprints: list[ServiceFingerprint] = []
        events: list[CyberEvent] = []
        dry_run = self.config.dry_run if self.dry_run_override is None else self.dry_run_override
        if camera_event.target_ip:
            # Scope validation happens even in dry-run so an operator cannot
            # use simulation to bypass the allowlist contract.
            try:
                self.scope_guard.require_allowed(camera_event.target_ip)
            except Exception as exc:
                self._set_state(CyberState.ERROR)
                return PipelineReport(self.state.value, tuple(self.state_history), errors=("DENIED_OUTSIDE_SCOPE",), dry_run=dry_run)

        self._set_state(CyberState.DISCOVERING)
        try:
            if camera_event.target_ip:
                assets = [DiscoveredAsset(camera_event.target_ip, discovery_method="explicit_target")]
            elif dry_run:
                assets = [DiscoveredAsset("192.168.56.20", discovery_method="dry_run_mock")]
            else:
                assets = self.discovery.discover()
            for asset in assets:
                if self.storage:
                    self.storage.save_asset(asset)
        except Exception as exc:
            errors.append(f"discovery:{type(exc).__name__}")

        self._set_state(CyberState.CORRELATING)
        correlations: list[CorrelationResult] = []
        try:
            correlations = self._select_correlations(camera_event, assets, fingerprints)
        except Exception as exc:
            errors.append(f"correlation:{type(exc).__name__}")

        self._set_state(CyberState.FINGERPRINTING)
        try:
            if dry_run:
                fingerprints = [self._mock_fingerprint(asset.ip) for asset in assets]
            elif assets and self.config.fingerprint.enabled:
                fingerprints = self.fingerprinter.fingerprint_assets(assets)
            for item in fingerprints:
                if self.storage:
                    self.storage.save_service(item)
        except Exception as exc:
            errors.append(f"fingerprinting:{type(exc).__name__}")
        # Correlation needs service evidence, so refresh it after fingerprinting.
        try:
            correlations = self._select_correlations(camera_event, assets, fingerprints)
        except Exception as exc:
            errors.append(f"correlation_refresh:{type(exc).__name__}")

        self._set_state(CyberState.MATCHING_VULNS)
        findings: list[VulnerabilityFinding] = []
        try:
            if not dry_run:
                findings = self.matcher.match(fingerprints)
        except Exception as exc:
            errors.append(f"vulnerability_matching:{type(exc).__name__}")

        self._set_state(CyberState.VALIDATING)
        validations: list[ValidationResult] = []
        try:
            if dry_run:
                validations = [ValidationResult("dry_run", "SKIPPED", {"network_suppressed": True})]
            else:
                validations = self.validator.validate(fingerprints)
        except Exception as exc:
            errors.append(f"validation:{type(exc).__name__}")

        self._set_state(CyberState.SCORING)
        try:
            best_correlation = correlations[0] if correlations else None
            risk = self.risk_engine.assess(
                findings,
                validations,
                device_class=camera_event.device_class,
                service_reachable=bool(fingerprints),
                correlation_confidence=best_correlation.confidence if best_correlation else camera_event.confidence,
            )
            log_event(self.logger, "risk_calculated", score=risk.score, severity=risk.severity)
        except Exception as exc:
            errors.append(f"risk:{type(exc).__name__}")
            risk = RiskAssessment(0.0, "low", ("risk calculation failed closed",))

        grouped: dict[tuple[str, int, str], list[ServiceFingerprint]] = {}
        for item in fingerprints:
            grouped.setdefault((item.ip, item.port, item.service), []).append(item)
        for ip, port, service in grouped:
            correlation = next((item for item in correlations if item.asset.ip == ip), None)
            finding = next((item for item in findings if item.matched_service == service), None)
            event = CyberEvent(
                agent_id=camera_event.agent_id,
                camera_id=camera_event.camera_id,
                target_ip=ip,
                device_class=camera_event.device_class,
                service=service,
                port=port,
                vulnerability=finding,
                validation=tuple(validations),
                risk=risk,
                correlation=correlation,
                timestamp=camera_event.timestamp,
            )
            events.append(event)
            if self.storage:
                self.storage.save_event(event, pending=not dry_run)

        if not grouped and assets:
            # A reachable host with no configured service is still a useful,
            # low-risk audit result and must not disappear from telemetry.
            for asset in assets:
                correlation = next((item for item in correlations if item.asset.ip == asset.ip), None)
                event = CyberEvent(
                    agent_id=camera_event.agent_id,
                    camera_id=camera_event.camera_id,
                    target_ip=asset.ip,
                    device_class=camera_event.device_class,
                    validation=tuple(validations),
                    risk=risk,
                    correlation=correlation,
                    timestamp=camera_event.timestamp,
                )
                events.append(event)
                if self.storage:
                    self.storage.save_event(event, pending=not dry_run)

        self._set_state(CyberState.PUBLISHING)
        if self.mainflux and events and not dry_run:
            for event in events:
                try:
                    if self.mainflux.publish(event) and self.storage:
                        self.storage.mark_outbox_sent(event.target_ip, self.storage.event_payload(event))
                except Exception as exc:
                    errors.append(f"mainflux:{type(exc).__name__}")
                    self.logger.error("mainflux_publish_failed error=%s", type(exc).__name__)
        self._set_state(CyberState.ERROR if errors and not events else CyberState.DONE)
        return PipelineReport(
            state=self.state.value,
            state_history=tuple(self.state_history),
            events=tuple(events),
            discovered_assets=tuple(assets),
            fingerprints=tuple(fingerprints),
            errors=tuple(errors),
            dry_run=dry_run,
        )


__all__ = ["CyberOrchestrator", "CyberState"]
