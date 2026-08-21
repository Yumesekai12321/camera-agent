from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
from typing import Any

import yaml

from ..config import PROJECT_DIR


class CyberConfigurationError(ValueError):
    """Raised when a cyber-agent configuration is unsafe or malformed."""


def _bool(value: Any, name: str, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in {"1", "true", "yes", "on"}:
        return True
    if isinstance(value, str) and value.strip().lower() in {"0", "false", "no", "off"}:
        return False
    raise CyberConfigurationError(f"{name} must be boolean")


def _float(value: Any, name: str, default: float, minimum: float = 0.0) -> float:
    try:
        result = default if value is None else float(value)
    except (TypeError, ValueError) as exc:
        raise CyberConfigurationError(f"{name} must be a number") from exc
    if result < minimum:
        raise CyberConfigurationError(f"{name} must be >= {minimum}")
    return result


def _int(value: Any, name: str, default: int, minimum: int = 0) -> int:
    try:
        result = default if value is None else int(value)
    except (TypeError, ValueError) as exc:
        raise CyberConfigurationError(f"{name} must be an integer") from exc
    if result < minimum:
        raise CyberConfigurationError(f"{name} must be >= {minimum}")
    return result


def _strings(value: Any, name: str, default: tuple[str, ...] = ()) -> tuple[str, ...]:
    if value is None:
        return default
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise CyberConfigurationError(f"{name} must be a list of non-empty strings")
    return tuple(item.strip() for item in value)


@dataclass(frozen=True)
class ScopeSettings:
    allowed_subnets: tuple[str, ...] = ()
    allowed_hosts: tuple[str, ...] = ()
    denied_hosts: tuple[str, ...] = ()
    deny_public_ips: bool = True


@dataclass(frozen=True)
class DiscoverySettings:
    enabled: bool = True
    methods: tuple[str, ...] = ("icmp", "arp", "mdns", "ssdp")
    timeout_seconds: float = 1.5
    max_hosts_per_minute: int = 60
    max_concurrency: int = 8
    max_hosts: int = 256
    fallback_ports: tuple[int, ...] = (22, 80, 443, 554, 1883, 8080, 8443)


@dataclass(frozen=True)
class FingerprintSettings:
    enabled: bool = True
    ports: tuple[int, ...] = (21, 22, 23, 53, 80, 443, 554, 1883, 8080, 8443)
    connect_timeout_seconds: float = 1.5
    banner_timeout_seconds: float = 1.5
    max_banner_bytes: int = 512
    max_connections_per_minute: int = 120


@dataclass(frozen=True)
class VulnerabilitySettings:
    enabled: bool = True
    sources: tuple[str, ...] = ("nvd", "cisa_kev")
    cache_ttl_hours: float = 24.0
    cache_dir: Path = PROJECT_DIR / "data" / "cve_cache"
    allow_remote_sources: bool = False
    max_remote_requests_per_minute: int = 30


@dataclass(frozen=True)
class ValidationSettings:
    enabled: bool = True
    allow_safe_http_probe: bool = True
    allow_tls_probe: bool = True
    allow_banner_probe: bool = True
    allow_unauthenticated_endpoint_check: bool = True
    allow_default_config_check: bool = True
    paths: tuple[str, ...] = ("/", "/health", "/status", "/admin")
    max_requests_per_minute: int = 120
    allow_exploit: bool = False
    allow_bruteforce: bool = False
    allow_password_spray: bool = False


@dataclass(frozen=True)
class RiskSettings:
    cvss: float = 0.4
    exploitability: float = 0.2
    exposure: float = 0.2
    asset_value: float = 0.2

    def validate(self) -> None:
        values = (self.cvss, self.exploitability, self.exposure, self.asset_value)
        if any(value < 0 for value in values) or sum(values) <= 0:
            raise CyberConfigurationError("risk weights must be non-negative and non-zero")


@dataclass(frozen=True)
class MainfluxSettings:
    enabled: bool = True
    base_url: str = "http://localhost"
    http_adapter_url: str = "http://localhost:8185"
    token_env: str = "MAINFLUX_TOKEN"
    thing_id_env: str = "MAINFLUX_THING_ID"
    channel_id_env: str = "MAINFLUX_CHANNEL_ID"
    timeout_seconds: float = 5.0
    retry_backoff_seconds: float = 5.0
    messages_path: str = "/http/messages"


@dataclass(frozen=True)
class LoggingSettings:
    json_logs: bool = True
    file: Path = PROJECT_DIR / "logs" / "cyber_agent.jsonl"


@dataclass(frozen=True)
class CyberConfig:
    enabled: bool = True
    mode: str = "safe"
    scope: ScopeSettings = field(default_factory=ScopeSettings)
    discovery: DiscoverySettings = field(default_factory=DiscoverySettings)
    fingerprint: FingerprintSettings = field(default_factory=FingerprintSettings)
    vulnerability: VulnerabilitySettings = field(default_factory=VulnerabilitySettings)
    validation: ValidationSettings = field(default_factory=ValidationSettings)
    risk: RiskSettings = field(default_factory=RiskSettings)
    mainflux: MainfluxSettings = field(default_factory=MainfluxSettings)
    logging: LoggingSettings = field(default_factory=LoggingSettings)
    source_path: Path | None = None

    @property
    def dry_run(self) -> bool:
        return self.mode == "dry-run"

    @classmethod
    def from_yaml(cls, path: Path | None = None) -> "CyberConfig":
        path = path or PROJECT_DIR / "config" / "cyber_agent.yaml"
        try:
            with path.open("r", encoding="utf-8") as handle:
                raw = yaml.safe_load(handle) or {}
        except (OSError, yaml.YAMLError) as exc:
            raise CyberConfigurationError(f"could not read cyber config {path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise CyberConfigurationError("cyber config root must be an object")

        mode = str(raw.get("mode", "safe")).strip().lower()
        if mode not in {"safe", "dry-run"}:
            raise CyberConfigurationError("mode must be safe or dry-run")
        scope_raw = raw.get("scope", {})
        discovery_raw = raw.get("discovery", {})
        fingerprint_raw = raw.get("fingerprint", {})
        vulnerability_raw = raw.get("vulnerability", {})
        validation_raw = raw.get("validation", {})
        risk_section = raw.get("risk", {})
        risk_raw = risk_section.get("weights", risk_section) if isinstance(risk_section, dict) else risk_section
        mainflux_raw = raw.get("mainflux", {})
        logging_raw = raw.get("logging", {})
        for name, value in (
            ("scope", scope_raw),
            ("discovery", discovery_raw),
            ("fingerprint", fingerprint_raw),
            ("vulnerability", vulnerability_raw),
            ("validation", validation_raw),
            ("risk", risk_raw),
            ("mainflux", mainflux_raw),
            ("logging", logging_raw),
        ):
            if not isinstance(value, dict):
                raise CyberConfigurationError(f"{name} must be an object")

        scope = ScopeSettings(
            allowed_subnets=_strings(scope_raw.get("allowed_subnets"), "scope.allowed_subnets"),
            allowed_hosts=_strings(scope_raw.get("allowed_hosts"), "scope.allowed_hosts"),
            denied_hosts=_strings(scope_raw.get("denied_hosts"), "scope.denied_hosts"),
            deny_public_ips=_bool(scope_raw.get("deny_public_ips"), "scope.deny_public_ips", True),
        )
        discovery = DiscoverySettings(
            enabled=_bool(discovery_raw.get("enabled"), "discovery.enabled", True),
            methods=_strings(discovery_raw.get("methods"), "discovery.methods", DiscoverySettings.methods),
            timeout_seconds=_float(discovery_raw.get("timeout_seconds"), "discovery.timeout_seconds", 1.5, 0.1),
            max_hosts_per_minute=_int(discovery_raw.get("max_hosts_per_minute"), "discovery.max_hosts_per_minute", 60, 1),
            max_concurrency=_int(discovery_raw.get("max_concurrency"), "discovery.max_concurrency", 8, 1),
            max_hosts=_int(discovery_raw.get("max_hosts"), "discovery.max_hosts", 256, 1),
            fallback_ports=tuple(_int(value, "discovery.fallback_ports", 0, 1) for value in (discovery_raw.get("fallback_ports") or DiscoverySettings.fallback_ports)),
        )
        fingerprint = FingerprintSettings(
            enabled=_bool(fingerprint_raw.get("enabled"), "fingerprint.enabled", True),
            ports=tuple(_int(value, "fingerprint.ports", 0, 1) for value in (fingerprint_raw.get("ports") or FingerprintSettings.ports)),
            connect_timeout_seconds=_float(fingerprint_raw.get("connect_timeout_seconds"), "fingerprint.connect_timeout_seconds", 1.5, 0.1),
            banner_timeout_seconds=_float(fingerprint_raw.get("banner_timeout_seconds"), "fingerprint.banner_timeout_seconds", 1.5, 0.1),
            max_banner_bytes=_int(fingerprint_raw.get("max_banner_bytes"), "fingerprint.max_banner_bytes", 512, 64),
            max_connections_per_minute=_int(fingerprint_raw.get("max_connections_per_minute"), "fingerprint.max_connections_per_minute", 120, 1),
        )
        cache_dir = Path(vulnerability_raw.get("cache_dir", PROJECT_DIR / "data" / "cve_cache")).expanduser()
        if not cache_dir.is_absolute():
            cache_dir = PROJECT_DIR / cache_dir
        vulnerability = VulnerabilitySettings(
            enabled=_bool(vulnerability_raw.get("enabled"), "vulnerability.enabled", True),
            sources=_strings(vulnerability_raw.get("sources"), "vulnerability.sources", VulnerabilitySettings.sources),
            cache_ttl_hours=_float(vulnerability_raw.get("cache_ttl_hours"), "vulnerability.cache_ttl_hours", 24.0, 0.0),
            cache_dir=cache_dir,
            allow_remote_sources=_bool(vulnerability_raw.get("allow_remote_sources"), "vulnerability.allow_remote_sources", False),
            max_remote_requests_per_minute=_int(vulnerability_raw.get("max_remote_requests_per_minute"), "vulnerability.max_remote_requests_per_minute", 30, 1),
        )
        validation = ValidationSettings(
            enabled=_bool(validation_raw.get("enabled"), "validation.enabled", True),
            allow_safe_http_probe=_bool(validation_raw.get("allow_safe_http_probe"), "validation.allow_safe_http_probe", True),
            allow_tls_probe=_bool(validation_raw.get("allow_tls_probe"), "validation.allow_tls_probe", True),
            allow_banner_probe=_bool(validation_raw.get("allow_banner_probe"), "validation.allow_banner_probe", True),
            allow_unauthenticated_endpoint_check=_bool(validation_raw.get("allow_unauthenticated_endpoint_check"), "validation.allow_unauthenticated_endpoint_check", True),
            allow_default_config_check=_bool(validation_raw.get("allow_default_config_check"), "validation.allow_default_config_check", True),
            paths=_strings(validation_raw.get("paths"), "validation.paths", ValidationSettings.paths),
            max_requests_per_minute=_int(validation_raw.get("max_requests_per_minute"), "validation.max_requests_per_minute", 120, 1),
            allow_exploit=_bool(validation_raw.get("allow_exploit"), "validation.allow_exploit", False),
            allow_bruteforce=_bool(validation_raw.get("allow_bruteforce"), "validation.allow_bruteforce", False),
            allow_password_spray=_bool(validation_raw.get("allow_password_spray"), "validation.allow_password_spray", False),
        )
        if validation.allow_exploit or validation.allow_bruteforce or validation.allow_password_spray:
            raise CyberConfigurationError("exploit, brute-force and password spraying are permanently disabled")
        weights = RiskSettings(
            cvss=_float(risk_raw.get("cvss"), "risk.weights.cvss", 0.4, 0.0),
            exploitability=_float(risk_raw.get("exploitability"), "risk.weights.exploitability", 0.2, 0.0),
            exposure=_float(risk_raw.get("exposure"), "risk.weights.exposure", 0.2, 0.0),
            asset_value=_float(risk_raw.get("asset_value"), "risk.weights.asset_value", 0.2, 0.0),
        )
        weights.validate()
        mainflux = MainfluxSettings(
            enabled=_bool(mainflux_raw.get("enabled"), "mainflux.enabled", True),
            base_url=str(mainflux_raw.get("base_url", "http://localhost")).rstrip("/"),
            http_adapter_url=str(mainflux_raw.get("http_adapter_url", "http://localhost:8185")).rstrip("/"),
            token_env=str(mainflux_raw.get("token_env", "MAINFLUX_TOKEN")),
            thing_id_env=str(mainflux_raw.get("thing_id_env", "MAINFLUX_THING_ID")),
            channel_id_env=str(mainflux_raw.get("channel_id_env", "MAINFLUX_CHANNEL_ID")),
            timeout_seconds=_float(mainflux_raw.get("timeout_seconds"), "mainflux.timeout_seconds", 5.0, 0.1),
            retry_backoff_seconds=_float(mainflux_raw.get("retry_backoff_seconds"), "mainflux.retry_backoff_seconds", 5.0, 0.1),
            messages_path=str(mainflux_raw.get("messages_path", "/http/messages")),
        )
        log_file = Path(logging_raw.get("file", PROJECT_DIR / "logs" / "cyber_agent.jsonl")).expanduser()
        if not log_file.is_absolute():
            log_file = PROJECT_DIR / log_file
        config = cls(
            enabled=_bool(raw.get("enabled"), "enabled", True),
            mode=mode,
            scope=scope,
            discovery=discovery,
            fingerprint=fingerprint,
            vulnerability=vulnerability,
            validation=validation,
            risk=weights,
            mainflux=mainflux,
            logging=LoggingSettings(
                json_logs=_bool(logging_raw.get("json_logs"), "logging.json_logs", True),
                file=log_file,
            ),
            source_path=path,
        )
        return config

    def resolved_mainflux_env(self) -> dict[str, str | None]:
        """Resolve references without ever returning or logging unrelated secrets."""
        return {
            "token": os.environ.get(self.mainflux.token_env),
            "thing_id": os.environ.get(self.mainflux.thing_id_env),
            "channel_id": os.environ.get(self.mainflux.channel_id_env),
        }

    def safe_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "mode": self.mode,
            "scope": {
                "allowed_subnets": list(self.scope.allowed_subnets),
                "allowed_hosts": list(self.scope.allowed_hosts),
                "denied_hosts": list(self.scope.denied_hosts),
                "deny_public_ips": self.scope.deny_public_ips,
            },
            "discovery": {"enabled": self.discovery.enabled, "methods": list(self.discovery.methods), "max_hosts": self.discovery.max_hosts},
            "fingerprint": {"enabled": self.fingerprint.enabled, "ports": list(self.fingerprint.ports)},
            "vulnerability": {"enabled": self.vulnerability.enabled, "sources": list(self.vulnerability.sources), "remote_sources": self.vulnerability.allow_remote_sources},
            "validation": {"enabled": self.validation.enabled, "safe_probes": True, "exploit": False, "bruteforce": False, "password_spray": False},
            "mainflux": {"enabled": self.mainflux.enabled, "base_url": self.mainflux.base_url, "http_adapter_url": self.mainflux.http_adapter_url, "token_env": self.mainflux.token_env, "thing_id_env": self.mainflux.thing_id_env, "channel_id_env": self.mainflux.channel_id_env},
            "logging": {"json_logs": self.logging.json_logs, "file": str(self.logging.file)},
        }


__all__ = [
    "CyberConfig",
    "CyberConfigurationError",
    "DiscoverySettings",
    "FingerprintSettings",
    "MainfluxSettings",
    "RiskSettings",
    "ScopeSettings",
    "ValidationSettings",
    "VulnerabilitySettings",
]
