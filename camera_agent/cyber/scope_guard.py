from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import logging
from collections.abc import Iterable

from .config import ScopeSettings


LOGGER = logging.getLogger(__name__)


class OutOfScopeTargetError(ValueError):
    """Raised before a network operation for a target outside the allowlist."""


@dataclass(frozen=True)
class _ParsedScope:
    subnets: tuple[ipaddress._BaseNetwork, ...]
    hosts: frozenset[ipaddress._BaseAddress]
    denied: frozenset[ipaddress._BaseAddress]


class ScopeGuard:
    """Central allowlist gate used by every cyber network adapter."""

    def __init__(self, settings: ScopeSettings, *, logger: logging.Logger | None = None) -> None:
        self.settings = settings
        self.logger = logger or LOGGER
        try:
            subnets = tuple(ipaddress.ip_network(value, strict=False) for value in settings.allowed_subnets)
            hosts = frozenset(ipaddress.ip_address(value) for value in settings.allowed_hosts)
            denied = frozenset(ipaddress.ip_address(value) for value in settings.denied_hosts)
        except ValueError as exc:
            raise ValueError(f"invalid scope IP/network: {exc}") from exc
        self._scope = _ParsedScope(subnets, hosts, denied)

    @staticmethod
    def _parse(ip: str) -> ipaddress._BaseAddress | None:
        try:
            return ipaddress.ip_address(str(ip).strip())
        except ValueError:
            return None

    def is_private_ip(self, ip: str) -> bool:
        address = self._parse(ip)
        return bool(address is not None and address.is_private)

    def is_denied(self, ip: str) -> bool:
        address = self._parse(ip)
        if address is None:
            return True
        return address in self._scope.denied

    def is_allowed_ip(self, ip: str) -> bool:
        address = self._parse(ip)
        if address is None or self.is_denied(str(address)):
            return False
        if self.settings.deny_public_ips and address.is_global:
            return False
        return address in self._scope.hosts or any(address in network for network in self._scope.subnets)

    def require_allowed(self, ip: str) -> str:
        normalized = str(ip).strip()
        if not self.is_allowed_ip(normalized):
            self.logger.warning(
                "DENIED_OUTSIDE_SCOPE ip=%s reason=outside_allowlist",
                normalized[:64],
            )
            raise OutOfScopeTargetError("DENIED_OUTSIDE_SCOPE")
        return str(self._parse(normalized))

    def require_allowed_many(self, ips: Iterable[str]) -> tuple[str, ...]:
        return tuple(self.require_allowed(ip) for ip in ips)

    def require_multicast(self, ip: str) -> str:
        """Authorize only the fixed private-LAN discovery multicast addresses."""
        address = self._parse(ip)
        allowed = {"224.0.0.251", "239.255.255.250"}
        if address is None or str(address) not in allowed:
            self.logger.warning("DENIED_OUTSIDE_SCOPE ip=%s reason=invalid_multicast", str(ip)[:64])
            raise OutOfScopeTargetError("DENIED_OUTSIDE_SCOPE")
        if not self._scope.subnets and not self._scope.hosts:
            raise OutOfScopeTargetError("DENIED_OUTSIDE_SCOPE")
        return str(address)


__all__ = ["OutOfScopeTargetError", "ScopeGuard"]
