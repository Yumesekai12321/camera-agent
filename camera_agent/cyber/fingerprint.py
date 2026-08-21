from __future__ import annotations

from dataclasses import dataclass
import logging
import re
import socket
import ssl
from collections.abc import Callable

from .config import FingerprintSettings
from .discovery import RateLimiter
from .logging_utils import log_event
from .models import DiscoveredAsset, ServiceFingerprint
from .scope_guard import ScopeGuard


LOGGER = logging.getLogger(__name__)
_VERSION_RE = re.compile(r"(?:server|ssh-2\.0|ftp|rtsp)[/:\s-]*([A-Za-z0-9_.+/-]{2,64})", re.I)


def _service_for_port(port: int) -> tuple[str, str, bool]:
    mapping = {
        21: ("ftp", "tcp", False),
        22: ("ssh", "tcp", False),
        23: ("telnet", "tcp", False),
        53: ("dns", "tcp", False),
        80: ("http", "http", False),
        443: ("https", "https", True),
        554: ("rtsp", "rtsp", False),
        1883: ("mqtt", "mqtt", False),
        8080: ("http-alt", "http", False),
        8443: ("https-alt", "https", True),
    }
    return mapping.get(port, ("unknown", "tcp", False))


def _safe_text(data: bytes, limit: int) -> str | None:
    if not data:
        return None
    text = data[:limit].decode("latin-1", errors="replace")
    lines = []
    for line in text.replace("\x00", "").splitlines():
        if not line.strip():
            continue
        lower = line.lower()
        if any(secret in lower for secret in ("authorization:", "cookie:", "set-cookie:", "password", "token=")):
            continue
        if line.lower().startswith(("server:", "ssh-", "220 ", "rtsp/", "http/")):
            lines.append(line.strip())
    if not lines:
        return None
    return " | ".join(lines)[:limit]


def _extract_version(banner: str | None) -> str | None:
    if not banner:
        return None
    match = _VERSION_RE.search(banner)
    if match:
        return match.group(1)[:64]
    return banner[:64]


@dataclass
class ServiceFingerprinter:
    settings: FingerprintSettings
    scope_guard: ScopeGuard
    connector: Callable[[tuple[str, int], float], object] = socket.create_connection
    logger: logging.Logger = LOGGER

    def __post_init__(self) -> None:
        self._rate_limiter = RateLimiter(self.settings.max_connections_per_minute)

    def _http_probe(self, connection: object, ip: str, port: int, *, tls: bool) -> bytes:
        request = f"HEAD / HTTP/1.1\r\nHost: {ip}\r\nConnection: close\r\n\r\n".encode("ascii")
        sendall = getattr(connection, "sendall")
        sendall(request)
        return getattr(connection, "recv")(self.settings.max_banner_bytes)

    def _rtsp_probe(self, connection: object, ip: str) -> bytes:
        request = f"OPTIONS rtsp://{ip}/ RTSP/1.0\r\nCSeq: 1\r\nUser-Agent: camera-agent-safe-probe\r\n\r\n".encode("ascii")
        getattr(connection, "sendall")(request)
        return getattr(connection, "recv")(self.settings.max_banner_bytes)

    def _tls_wrap(self, connection: object, ip: str) -> object:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        return context.wrap_socket(connection, server_hostname=ip)

    def fingerprint_host(self, ip: str) -> list[ServiceFingerprint]:
        results: list[ServiceFingerprint] = []
        for port in self.settings.ports:
            self._rate_limiter.wait()
            self.scope_guard.require_allowed(ip)
            connection = None
            wrapped = None
            service, protocol, tls_expected = _service_for_port(port)
            try:
                connection = self.connector((ip, port), self.settings.connect_timeout_seconds)
                wrapped = self._tls_wrap(connection, ip) if tls_expected else connection
                banner_data = b""
                if service in {"http", "http-alt", "https", "https-alt"}:
                    banner_data = self._http_probe(wrapped, ip, port, tls=tls_expected)
                elif service == "rtsp":
                    banner_data = self._rtsp_probe(wrapped, ip)
                else:
                    try:
                        banner_data = getattr(wrapped, "recv")(self.settings.max_banner_bytes)
                    except (OSError, TimeoutError):
                        banner_data = b""
                banner = _safe_text(banner_data, self.settings.max_banner_bytes)
                version = _extract_version(banner)
                fingerprint = ServiceFingerprint(
                    ip=ip,
                    port=port,
                    protocol=protocol,
                    service=service,
                    version=version,
                    banner=banner,
                    tls=tls_expected,
                    confidence=0.85 if service != "unknown" else 0.45,
                )
                results.append(fingerprint)
                log_event(self.logger, "service_found", ip=ip, port=port, service=service)
            except (OSError, TimeoutError, ssl.SSLError):
                self.logger.debug("fingerprint connect failed ip=%s port=%s", ip[:64], port)
                continue
            finally:
                for resource in (wrapped, connection):
                    close = getattr(resource, "close", None)
                    if close is not None:
                        try:
                            close()
                        except OSError:
                            pass
        return results

    def fingerprint_assets(self, assets: list[DiscoveredAsset]) -> list[ServiceFingerprint]:
        if not self.settings.enabled:
            return []
        fingerprints: list[ServiceFingerprint] = []
        for asset in assets:
            fingerprints.extend(self.fingerprint_host(asset.ip))
        return fingerprints


__all__ = ["ServiceFingerprinter"]
