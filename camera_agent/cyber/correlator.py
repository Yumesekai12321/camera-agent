from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import logging
from collections.abc import Mapping

from .logging_utils import log_event
from .models import CorrelationResult, DiscoveredAsset, ServiceFingerprint


LOGGER = logging.getLogger(__name__)


@dataclass
class AssetCorrelator:
    """Correlate visual and network evidence without asserting identity."""

    logger: logging.Logger = LOGGER

    _vendor_hints = {
        "router": ("tp-link", "tplink", "netgear", "asus", "mikrotik", "cisco", "ubiquiti"),
        "camera": ("hikvision", "dahua", "tp-link", "tapo", "axis", "reolink"),
        "iot": ("espressif", "shelly", "sonoff", "tuya", "tapo"),
        "laptop": ("intel", "dell", "lenovo", "hp", "apple", "microsoft"),
        "phone": ("samsung", "xiaomi", "apple", "google", "huawei"),
    }
    _service_hints = {
        "router": {80, 443, 53, 22, 8080, 8443},
        "camera": {80, 443, 554, 8080, 8443},
        "iot": {80, 443, 1883, 8080, 8443},
        "laptop": {22, 80, 443, 3389},
        "phone": {80, 443, 5555},
        "monitor": {80, 443},
    }

    @staticmethod
    def _timing_score(asset: DiscoveredAsset, observed_at: str | None) -> float:
        if not observed_at:
            return 0.0
        try:
            left = datetime.fromisoformat(asset.last_seen.replace("Z", "+00:00"))
            right = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
            seconds = abs((left - right).total_seconds())
            return 1.0 if seconds <= 5 else 0.5 if seconds <= 30 else 0.0
        except ValueError:
            return 0.0

    def correlate(
        self,
        device_class: str,
        assets: list[DiscoveredAsset],
        fingerprints: Mapping[str, list[ServiceFingerprint]] | list[ServiceFingerprint],
        *,
        observed_at: str | None = None,
    ) -> list[CorrelationResult]:
        normalized_class = (device_class or "unknown").strip().casefold()
        if isinstance(fingerprints, list):
            by_ip: dict[str, list[ServiceFingerprint]] = {}
            for fingerprint in fingerprints:
                by_ip.setdefault(fingerprint.ip, []).append(fingerprint)
        else:
            by_ip = dict(fingerprints)
        results: list[CorrelationResult] = []
        for asset in assets:
            score = 0.0
            reasons: list[str] = []
            vendor = (asset.vendor or "").casefold()
            hostname = (asset.hostname or "").casefold()
            if normalized_class in self._vendor_hints and any(hint in vendor or hint in hostname for hint in self._vendor_hints[normalized_class]):
                score += 0.50
                reasons.append("vendor or hostname matches visual device class")
            ports = {fingerprint.port for fingerprint in by_ip.get(asset.ip, [])}
            expected_ports = self._service_hints.get(normalized_class, set())
            if expected_ports and ports & expected_ports:
                score += 0.35
                reasons.append("reachable service is compatible with visual device class")
            timing = self._timing_score(asset, observed_at)
            if timing:
                score += 0.15 * timing
                reasons.append("discovery timing is close to the camera observation")
            if asset.mac:
                score += 0.10
                reasons.append("MAC metadata is available for audit correlation")
            score = round(min(1.0, score), 2)
            status = "CONFIRMED" if score >= 0.8 else "UNCONFIRMED"
            if not reasons:
                reasons.append("insufficient vendor, service or timing evidence")
            result = CorrelationResult(asset, normalized_class, score, status, tuple(reasons))
            results.append(result)
            log_event(self.logger, "asset_correlated", ip=asset.ip, device_class=normalized_class, confidence=score, status=status)
        return sorted(results, key=lambda result: result.confidence, reverse=True)


__all__ = ["AssetCorrelator"]
