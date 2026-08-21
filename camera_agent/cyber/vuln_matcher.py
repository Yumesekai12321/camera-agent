from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import re
from typing import Any

import requests

from .config import VulnerabilitySettings
from .discovery import RateLimiter
from .logging_utils import log_event
from .models import ServiceFingerprint, VulnerabilityFinding


LOGGER = logging.getLogger(__name__)
_CVE_RE = re.compile(r"CVE-\d{4}-\d{4,}", re.I)


def _severity(score: float) -> str:
    if score >= 9:
        return "critical"
    if score >= 7:
        return "high"
    if score >= 4:
        return "medium"
    if score > 0:
        return "low"
    return "unknown"


def _cvss(vulnerability: dict[str, Any]) -> float:
    metrics = vulnerability.get("metrics") or {}
    for key in ("cvssMetricV31", "cvssMetricV30", "cvssMetricV2"):
        entries = metrics.get(key) or []
        if entries and isinstance(entries[0], dict):
            value = entries[0].get("cvssData", {}).get("baseScore")
            try:
                return float(value)
            except (TypeError, ValueError):
                pass
    return 0.0


@dataclass
class VulnerabilityMatcher:
    settings: VulnerabilitySettings
    session: requests.Session | None = None
    logger: logging.Logger = LOGGER

    def __post_init__(self) -> None:
        self.session = self.session or requests.Session()
        self.settings.cache_dir.mkdir(parents=True, exist_ok=True)
        self._rate_limiter = RateLimiter(self.settings.max_remote_requests_per_minute)

    def _cache_path(self, source: str, key: str) -> Path:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
        return self.settings.cache_dir / f"{source}_{digest}.json"

    def _read_cache(self, path: Path) -> Any | None:
        try:
            modified = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            if datetime.now(timezone.utc) - modified > timedelta(hours=self.settings.cache_ttl_hours):
                return None
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return None

    def _write_cache(self, path: Path, value: Any) -> None:
        try:
            path.write_text(json.dumps(value, ensure_ascii=True), encoding="utf-8")
        except OSError as exc:
            self.logger.debug("vulnerability cache write failed error=%s", type(exc).__name__)

    def _nvd(self, service: ServiceFingerprint) -> list[dict[str, Any]]:
        key = f"{service.service}|{service.version or ''}"
        path = self._cache_path("nvd", key)
        cached = self._read_cache(path)
        if cached is not None:
            return list(cached) if isinstance(cached, list) else []
        if not self.settings.allow_remote_sources:
            return []
        self._rate_limiter.wait()
        params = {"keywordSearch": key, "resultsPerPage": "50"}
        headers = {}
        api_key = os.environ.get("NVD_API_KEY")
        if api_key:
            headers["apiKey"] = api_key
        try:
            response = self.session.get(
                "https://services.nvd.nist.gov/rest/json/cves/2.0",
                params=params,
                headers=headers,
                timeout=10,
            )
            response.raise_for_status()
            data = response.json()
            values = [item.get("cve", {}) for item in data.get("vulnerabilities", []) if isinstance(item, dict)]
            self._write_cache(path, values)
            return values
        except (requests.RequestException, ValueError, TypeError) as exc:
            self.logger.warning("NVD lookup unavailable error=%s", type(exc).__name__)
            return []

    def _cisa_kev(self) -> list[dict[str, Any]]:
        path = self.settings.cache_dir / "cisa_kev.json"
        cached = self._read_cache(path)
        if cached is not None:
            return list(cached) if isinstance(cached, list) else []
        if not self.settings.allow_remote_sources:
            return []
        self._rate_limiter.wait()
        try:
            response = self.session.get(
                "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json",
                timeout=10,
            )
            response.raise_for_status()
            values = response.json().get("vulnerabilities", [])
            self._write_cache(path, values)
            return list(values) if isinstance(values, list) else []
        except (requests.RequestException, ValueError, TypeError) as exc:
            self.logger.warning("CISA KEV lookup unavailable error=%s", type(exc).__name__)
            return []

    @staticmethod
    def _nvd_to_finding(raw: dict[str, Any], service: ServiceFingerprint, kev_ids: set[str]) -> VulnerabilityFinding | None:
        cve_id = str(raw.get("id", "")).upper()
        if not _CVE_RE.fullmatch(cve_id):
            return None
        descriptions = raw.get("descriptions") or []
        description = next((str(item.get("value", "")) for item in descriptions if item.get("lang") == "en"), "")
        configurations = json.dumps(raw.get("configurations", []), ensure_ascii=True).lower()
        haystack = f"{cve_id} {description} {configurations}".lower()
        service_match = service.service.lower() in haystack or service.protocol.lower() in haystack
        version_match = bool(service.version and service.version.lower() in haystack)
        if not service_match and not version_match:
            return None
        score = _cvss(raw)
        confidence = 0.92 if version_match and service_match else 0.58
        title = description.split(".", 1)[0].strip()[:240] or cve_id
        return VulnerabilityFinding(
            cve_id=cve_id,
            title=title,
            description=description[:1000],
            cvss=score,
            severity=_severity(score),
            source="nvd",
            matched_service=service.service,
            matched_version=service.version or "",
            cisa_kev=cve_id in kev_ids,
            confidence=confidence,
        )

    def match(self, fingerprints: list[ServiceFingerprint]) -> list[VulnerabilityFinding]:
        if not self.settings.enabled:
            return []
        kev_rows = self._cisa_kev() if "cisa_kev" in self.settings.sources else []
        kev_ids = {str(row.get("cveID", "")).upper() for row in kev_rows if isinstance(row, dict)}
        findings: dict[tuple[str, str, str], VulnerabilityFinding] = {}
        for fingerprint in fingerprints:
            if not fingerprint.version:
                continue
            rows = self._nvd(fingerprint) if "nvd" in self.settings.sources else []
            for raw in rows:
                if not isinstance(raw, dict):
                    continue
                finding = self._nvd_to_finding(raw, fingerprint, kev_ids)
                if finding is None:
                    continue
                # A KEV feed can independently confirm exploitation status for
                # a CVE returned by a local NVD cache.
                if finding.cve_id in kev_ids and not finding.cisa_kev:
                    finding = VulnerabilityFinding(**{**finding.__dict__, "cisa_kev": True})
                key = (fingerprint.ip, fingerprint.service, finding.cve_id)
                previous = findings.get(key)
                if previous is None or finding.confidence > previous.confidence:
                    findings[key] = finding
                    log_event(self.logger, "cve_matched", cve=finding.cve_id, service=fingerprint.service)
        return list(findings.values())


__all__ = ["VulnerabilityMatcher"]
