from __future__ import annotations

from dataclasses import dataclass

from .config import RiskSettings
from .models import RiskAssessment, ValidationResult, VulnerabilityFinding


def _severity(score: float) -> str:
    if score >= 9:
        return "critical"
    if score >= 7:
        return "high"
    if score >= 4:
        return "medium"
    return "low"


@dataclass
class RiskEngine:
    settings: RiskSettings

    def assess(
        self,
        findings: list[VulnerabilityFinding],
        validations: list[ValidationResult],
        *,
        device_class: str = "unknown",
        service_reachable: bool = True,
        correlation_confidence: float = 0.0,
    ) -> RiskAssessment:
        max_cvss = max((finding.cvss for finding in findings), default=0.0)
        cvss_component = max(0.0, min(10.0, max_cvss))
        exploitability_component = max(
            (10.0 if finding.cisa_kev else 8.0 if finding.severity == "critical" else 7.0 if finding.severity == "high" else 4.0 if finding.severity == "medium" else 2.0 for finding in findings),
            default=0.0,
        )
        exposure_component = 0.0
        reasons: list[str] = []
        if service_reachable:
            exposure_component += 4.0
            reasons.append("service reachable on the allowlisted LAN target")
        unauth = any(
            result.status == "FINDING" and "unauthenticated" in result.validation_type
            or result.status == "FINDING" and "authentication" in result.validation_type
            for result in validations
        )
        if unauth:
            exposure_component += 4.0
            reasons.append("safe validation indicates unauthenticated exposure")
        if any(result.status == "FINDING" and result.validation_type == "tls_certificate" for result in validations):
            exposure_component += 2.0
            reasons.append("TLS certificate validation needs review")
        exposure_component = min(10.0, exposure_component)
        asset_values = {
            "router": 8.0,
            "camera": 7.0,
            "iot": 6.0,
            "server": 8.0,
            "laptop": 5.0,
            "phone": 4.0,
            "monitor": 3.0,
        }
        asset_value_component = asset_values.get(device_class.casefold(), 3.0)
        if device_class.casefold() in asset_values:
            reasons.append(f"asset class {device_class} has value {asset_value_component:.1f}/10")
        else:
            reasons.append("asset class is unconfirmed; conservative neutral value applied")
        if any(finding.cisa_kev for finding in findings):
            reasons.append("at least one matched finding is listed in CISA KEV")
        if correlation_confidence < 0.8:
            reasons.append("camera-to-network correlation is unconfirmed")
        weights = self.settings
        denominator = weights.cvss + weights.exploitability + weights.exposure + weights.asset_value
        score = (
            cvss_component * weights.cvss
            + exploitability_component * weights.exploitability
            + exposure_component * weights.exposure
            + asset_value_component * weights.asset_value
        ) / denominator
        score = round(max(0.0, min(10.0, score)), 2)
        return RiskAssessment(
            score=score,
            severity=_severity(score),
            reasons=tuple(reasons),
            cvss_component=round(cvss_component, 2),
            exposure_component=round(exposure_component, 2),
            exploitability_component=round(exploitability_component, 2),
            asset_value_component=round(asset_value_component, 2),
        )


__all__ = ["RiskEngine"]
