from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class DiscoveredAsset:
    ip: str
    mac: str | None = None
    vendor: str | None = None
    hostname: str | None = None
    discovery_method: str = "unknown"
    first_seen: str = field(default_factory=utc_now)
    last_seen: str = field(default_factory=utc_now)


@dataclass(frozen=True)
class ServiceFingerprint:
    ip: str
    port: int
    protocol: str
    service: str
    version: str | None = None
    banner: str | None = None
    tls: bool = False
    confidence: float = 0.0


@dataclass(frozen=True)
class VulnerabilityFinding:
    cve_id: str
    title: str
    description: str = ""
    cvss: float = 0.0
    severity: str = "unknown"
    source: str = "unknown"
    matched_service: str = ""
    matched_version: str = ""
    cisa_kev: bool = False
    confidence: float = 0.0


@dataclass(frozen=True)
class ValidationResult:
    validation_type: str
    status: str
    evidence: dict[str, Any] = field(default_factory=dict)
    safe: bool = True
    timestamp: str = field(default_factory=utc_now)


@dataclass(frozen=True)
class RiskAssessment:
    score: float
    severity: str
    reasons: tuple[str, ...] = ()
    cvss_component: float = 0.0
    exposure_component: float = 0.0
    exploitability_component: float = 0.0
    asset_value_component: float = 0.0


@dataclass(frozen=True)
class CameraEvent:
    agent_id: str
    camera_id: str
    device_class: str = "unknown"
    confidence: float = 0.0
    target_ip: str | None = None
    timestamp: str = field(default_factory=utc_now)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CorrelationResult:
    asset: DiscoveredAsset
    device_class: str
    confidence: float
    status: str
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class CyberEvent:
    agent_id: str
    camera_id: str
    target_ip: str
    device_class: str
    service: str = ""
    port: int | None = None
    vulnerability: VulnerabilityFinding | None = None
    validation: tuple[ValidationResult, ...] = ()
    risk: RiskAssessment | None = None
    correlation: CorrelationResult | None = None
    timestamp: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PipelineReport:
    state: str
    state_history: tuple[str, ...]
    events: tuple[CyberEvent, ...] = ()
    discovered_assets: tuple[DiscoveredAsset, ...] = ()
    fingerprints: tuple[ServiceFingerprint, ...] = ()
    errors: tuple[str, ...] = ()
    dry_run: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
