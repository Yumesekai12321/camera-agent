from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import socket
import ssl
from collections.abc import Callable
from typing import Any

import requests

from .config import ValidationSettings
from .discovery import RateLimiter
from .logging_utils import log_event
from .models import ServiceFingerprint, ValidationResult, utc_now
from .scope_guard import ScopeGuard


LOGGER = logging.getLogger(__name__)
_SECURITY_HEADERS = {
    "content-security-policy",
    "x-content-type-options",
    "x-frame-options",
    "referrer-policy",
}


def _result(validation_type: str, status: str, **evidence: Any) -> ValidationResult:
    return ValidationResult(validation_type, status, evidence={key: value for key, value in evidence.items()}, safe=True)


@dataclass
class SafeValidator:
    settings: ValidationSettings
    scope_guard: ScopeGuard
    session: requests.Session | None = None
    connector: Callable[[tuple[str, int], float], object] = socket.create_connection
    logger: logging.Logger = LOGGER

    def __post_init__(self) -> None:
        self.session = self.session or requests.Session()
        self._rate_limiter = RateLimiter(self.settings.max_requests_per_minute)

    def _url(self, fingerprint: ServiceFingerprint, path: str) -> str:
        scheme = "https" if fingerprint.tls or fingerprint.service in {"https", "https-alt"} else "http"
        return f"{scheme}://{fingerprint.ip}:{fingerprint.port}{path}"

    def _head(self, fingerprint: ServiceFingerprint, path: str) -> requests.Response | None:
        self.scope_guard.require_allowed(fingerprint.ip)
        self._rate_limiter.wait()
        try:
            return self.session.head(
                self._url(fingerprint, path),
                timeout=1.5,
                allow_redirects=False,
                verify=False,
            )
        except requests.RequestException as exc:
            self.logger.debug("safe HTTP probe failed error=%s", type(exc).__name__)
            return None

    def _get_limited(self, fingerprint: ServiceFingerprint, path: str) -> tuple[int | None, bool, dict[str, str]]:
        self.scope_guard.require_allowed(fingerprint.ip)
        self._rate_limiter.wait()
        try:
            response = self.session.get(
                self._url(fingerprint, path),
                timeout=1.5,
                allow_redirects=False,
                verify=False,
                stream=True,
            )
            listing_marker = False
            try:
                chunk = next(response.iter_content(chunk_size=4096), b"")
                text = chunk.decode("latin-1", errors="ignore").lower()
                listing_marker = "index of /" in text
            finally:
                response.close()
            return response.status_code, listing_marker, {str(key).lower(): "" for key in response.headers}
        except requests.RequestException as exc:
            self.logger.debug("safe HTTP content probe failed error=%s", type(exc).__name__)
            return None, False, {}

    def _http_validations(self, fingerprint: ServiceFingerprint) -> list[ValidationResult]:
        results: list[ValidationResult] = []
        response = self._head(fingerprint, "/")
        if response is None:
            return [_result("http_probe", "ERROR", request_completed=False)]
        headers = {str(key).lower(): "" for key in response.headers}
        missing = sorted(_SECURITY_HEADERS - set(headers))
        results.append(_result("http_security_headers", "FINDING" if missing else "PASS", missing_headers=missing, status_code=response.status_code))
        if "server" in headers or "x-powered-by" in headers:
            results.append(_result("server_banner_disclosure", "FINDING", disclosed_headers=sorted(set(headers) & {"server", "x-powered-by"})))
        if response.status_code == 401 and "www-authenticate" in headers:
            results.append(_result("http_basic_auth_presence", "PASS", status_code=response.status_code, challenge_present=True))
        for path in self.settings.paths:
            if path == "/" or not path.startswith("/"):
                continue
            candidate = self._head(fingerprint, path)
            if candidate is None:
                continue
            if path in {"/admin", "/admin/"}:
                exposed = 200 <= candidate.status_code < 400 and candidate.status_code not in {401, 403}
                results.append(_result("admin_panel_exposure", "FINDING" if exposed else "PASS", path=path, status_code=candidate.status_code))
            if path in {"/health", "/status"}:
                unauthenticated = 200 <= candidate.status_code < 400
                results.append(_result("unauthenticated_endpoint_check", "FINDING" if unauthenticated else "PASS", path=path, status_code=candidate.status_code))
        if self.settings.allow_default_config_check:
            status, listing, _ = self._get_limited(fingerprint, "/")
            results.append(_result("directory_listing", "FINDING" if listing else "PASS", status_code=status or 0, listing_detected=listing))
        if results:
            log_event(self.logger, "validation_passed", ip=fingerprint.ip, service=fingerprint.service)
        return results

    def _tls_validation(self, fingerprint: ServiceFingerprint) -> list[ValidationResult]:
        if not fingerprint.tls:
            return []
        self.scope_guard.require_allowed(fingerprint.ip)
        self._rate_limiter.wait()
        raw = None
        wrapped = None
        try:
            raw = self.connector((fingerprint.ip, fingerprint.port), 1.5)
            context = ssl.create_default_context()
            try:
                wrapped = context.wrap_socket(raw, server_hostname=fingerprint.ip)
                verified = True
            except ssl.SSLCertVerificationError:
                verified = False
                if raw is not None:
                    raw.close()
                raw = self.connector((fingerprint.ip, fingerprint.port), 1.5)
                insecure_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                insecure_context.check_hostname = False
                insecure_context.verify_mode = ssl.CERT_NONE
                wrapped = insecure_context.wrap_socket(raw, server_hostname=fingerprint.ip)
            version = getattr(wrapped, "version", lambda: None)()
            cipher_info = getattr(wrapped, "cipher", lambda: ())() or ()
            cipher = cipher_info[0] if isinstance(cipher_info, tuple) and cipher_info else "unknown"
            certificate = getattr(wrapped, "getpeercert", lambda: {})() or {}
            expired = False
            not_after = certificate.get("notAfter") if isinstance(certificate, dict) else None
            if not_after:
                try:
                    expires = datetime.strptime(not_after, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
                    expired = expires <= datetime.now(timezone.utc)
                except ValueError:
                    pass
            return [_result("tls_certificate", "FINDING" if (not verified or expired) else "PASS", verified=verified, expired=expired, tls_version=version or "unknown", cipher=str(cipher)[:128])]
        except (OSError, TimeoutError, ssl.SSLError) as exc:
            return [_result("tls_certificate", "ERROR", error=type(exc).__name__)]
        finally:
            for item in (wrapped, raw):
                close = getattr(item, "close", None)
                if close is not None:
                    try:
                        close()
                    except OSError:
                        pass

    def _rtsp_validation(self, fingerprint: ServiceFingerprint) -> list[ValidationResult]:
        self.scope_guard.require_allowed(fingerprint.ip)
        self._rate_limiter.wait()
        connection = None
        try:
            connection = self.connector((fingerprint.ip, fingerprint.port), 1.5)
            request = f"OPTIONS rtsp://{fingerprint.ip}/ RTSP/1.0\r\nCSeq: 1\r\nUser-Agent: camera-agent-safe-probe\r\n\r\n".encode("ascii")
            connection.sendall(request)
            response = connection.recv(512).split(b"\r\n", 1)[0].decode("latin-1", errors="ignore")
            parts = response.split()
            status = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
            auth_required = status in {401, 403}
            return [_result("rtsp_authentication", "PASS" if auth_required else "FINDING", status_code=status, authentication_required=auth_required)]
        except (OSError, TimeoutError, ValueError) as exc:
            return [_result("rtsp_authentication", "ERROR", error=type(exc).__name__)]
        finally:
            if connection is not None:
                try:
                    connection.close()
                except OSError:
                    pass

    def validate(self, fingerprints: list[ServiceFingerprint]) -> list[ValidationResult]:
        if not self.settings.enabled:
            return []
        results: list[ValidationResult] = []
        for fingerprint in fingerprints:
            if fingerprint.service in {"http", "http-alt", "https", "https-alt"} and self.settings.allow_safe_http_probe:
                results.extend(self._http_validations(fingerprint))
            if fingerprint.tls and self.settings.allow_tls_probe:
                results.extend(self._tls_validation(fingerprint))
            if fingerprint.service == "rtsp" and self.settings.allow_banner_probe:
                results.extend(self._rtsp_validation(fingerprint))
        return results


__all__ = ["SafeValidator"]
