from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import ipaddress
import logging
import platform
import re
import socket
import subprocess
import threading
import time
from collections.abc import Callable, Iterable

from .config import DiscoverySettings
from .logging_utils import log_event
from .models import DiscoveredAsset, utc_now
from .scope_guard import OutOfScopeTargetError, ScopeGuard


LOGGER = logging.getLogger(__name__)


class RateLimiter:
    """Simple process-local limiter shared by all discovery worker threads."""

    def __init__(self, max_per_minute: int, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.interval = 60.0 / max(1, max_per_minute)
        self.clock = clock
        self._last = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = self.clock()
            delay = max(0.0, self.interval - (now - self._last))
            if delay:
                time.sleep(delay)
                now = self.clock()
            self._last = now


Probe = Callable[[str, float], bool]


@dataclass(frozen=True)
class DiscoveryResult:
    assets: tuple[DiscoveredAsset, ...]
    denied_count: int = 0
    attempted_hosts: int = 0


class NetworkDiscovery:
    """Bounded LAN discovery with explicit allowlist checks and fallbacks.

    ARP and ICMP raw-socket privileges are not assumed.  When unavailable,
    the implementation uses one bounded TCP connect per configured fallback
    port.  mDNS is passive/limited and SSDP sends one standard M-SEARCH only.
    """

    def __init__(
        self,
        settings: DiscoverySettings,
        scope_guard: ScopeGuard,
        *,
        probe: Probe | None = None,
        ping: Probe | None = None,
        clock: Callable[[], float] = time.monotonic,
        logger: logging.Logger | None = None,
    ) -> None:
        self.settings = settings
        self.guard = scope_guard
        self.probe = probe or self._tcp_probe
        self.ping = ping or self._system_ping
        self.clock = clock
        self.logger = logger or LOGGER
        self.rate_limiter = RateLimiter(settings.max_hosts_per_minute, clock=clock)

    def _candidate_ips(self) -> tuple[str, ...]:
        values: list[str] = []
        seen: set[str] = set()
        for raw in (*self.guard.settings.allowed_hosts, *self.guard.settings.allowed_subnets):
            try:
                parsed = ipaddress.ip_address(raw)
                candidates = (parsed,)
            except ValueError:
                network = ipaddress.ip_network(raw, strict=False)
                # Do not accidentally expand a large enterprise network.
                candidates = network.hosts()
            for address in candidates:
                ip = str(address)
                if ip in seen or not self.guard.is_allowed_ip(ip):
                    continue
                seen.add(ip)
                values.append(ip)
                if len(values) >= self.settings.max_hosts:
                    return tuple(values)
        return tuple(values)

    def _tcp_probe(self, ip: str, timeout: float) -> bool:
        for port in self.settings.fallback_ports:
            self.guard.require_allowed(ip)
            try:
                with socket.create_connection((ip, port), timeout=timeout):
                    return True
            except (OSError, TimeoutError):
                continue
        return False

    @staticmethod
    def _system_ping(ip: str, timeout: float) -> bool:
        milliseconds = max(1, int(timeout * 1000))
        if platform.system().lower().startswith("win"):
            command = ["ping", "-n", "1", "-w", str(milliseconds), ip]
        else:
            command = ["ping", "-c", "1", "-W", str(max(1, int(timeout))), ip]
        try:
            completed = subprocess.run(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=timeout + 0.5,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return completed.returncode == 0

    def _asset(self, ip: str, method: str) -> DiscoveredAsset:
        now = utc_now()
        hostname = None
        try:
            hostname = socket.gethostbyaddr(ip)[0]
        except (OSError, socket.herror):
            pass
        return DiscoveredAsset(
            ip=ip,
            hostname=hostname,
            discovery_method=method,
            first_seen=now,
            last_seen=now,
        )

    def _host_probe(self, ip: str, methods: tuple[str, ...]) -> DiscoveredAsset | None:
        self.guard.require_allowed(ip)
        for method in methods:
            self.rate_limiter.wait()
            try:
                if method in {"icmp", "arp"}:
                    detected = self.ping(ip, self.settings.timeout_seconds) if method == "icmp" else self.probe(ip, self.settings.timeout_seconds)
                elif method == "tcp":
                    detected = self.probe(ip, self.settings.timeout_seconds)
                else:
                    continue
            except OutOfScopeTargetError:
                raise
            except Exception as exc:  # One adapter failure must not stop discovery.
                self.logger.debug("discovery method failed method=%s error=%s", method, type(exc).__name__)
                detected = False
            if detected:
                log_event(self.logger, "host_discovered", ip=ip, method=method)
                return self._asset(ip, method)
        return None

    def _discover_ssdp(self) -> list[DiscoveredAsset]:
        destination = self.guard.require_multicast("239.255.255.250")
        message = (
            "M-SEARCH * HTTP/1.1\r\n"
            "HOST: 239.255.255.250:1900\r\n"
            "MAN: \"ssdp:discover\"\r\n"
            "MX: 1\r\n"
            "ST: ssdp:all\r\n\r\n"
        ).encode("ascii")
        assets: dict[str, DiscoveredAsset] = {}
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                sock.settimeout(self.settings.timeout_seconds)
                self.rate_limiter.wait()
                sock.sendto(message, (destination, 1900))
                while True:
                    try:
                        _data, address = sock.recvfrom(4096)
                    except socket.timeout:
                        break
                    ip = str(address[0])
                    if not self.guard.is_allowed_ip(ip):
                        self.logger.warning("DENIED_OUTSIDE_SCOPE ip=%s reason=ssdp_response", ip[:64])
                        continue
                    assets.setdefault(ip, self._asset(ip, "ssdp"))
        except (OSError, OutOfScopeTargetError) as exc:
            self.logger.debug("ssdp discovery unavailable error=%s", type(exc).__name__)
        return list(assets.values())

    def _discover_arp_cache(self) -> list[DiscoveredAsset]:
        """Read the local ARP cache without issuing an ARP sweep."""
        try:
            completed = subprocess.run(
                ["arp", "-a"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                check=False,
                timeout=self.settings.timeout_seconds + 0.5,
            )
        except (OSError, subprocess.TimeoutExpired):
            return []
        assets: list[DiscoveredAsset] = []
        pattern = re.compile(r"(\d{1,3}(?:\.\d{1,3}){3})\s+([0-9a-f]{2}(?:-[0-9a-f]{2}){5})", re.I)
        for match in pattern.finditer(completed.stdout or ""):
            ip, mac = match.groups()
            if not self.guard.is_allowed_ip(ip):
                self.logger.warning("DENIED_OUTSIDE_SCOPE ip=%s reason=arp_cache", ip[:64])
                continue
            now = utc_now()
            assets.append(DiscoveredAsset(ip, mac=mac.lower(), discovery_method="arp", first_seen=now, last_seen=now))
        return assets

    def _discover_mdns(self) -> list[DiscoveredAsset]:
        """Listen briefly for mDNS responses without querying arbitrary hosts."""
        destination = self.guard.require_multicast("224.0.0.251")
        assets: dict[str, DiscoveredAsset] = {}
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind(("", 5353))
                sock.settimeout(self.settings.timeout_seconds)
                # Referencing the fixed multicast address documents the
                # boundary and avoids a broadcast/query to arbitrary hosts.
                del destination
                while True:
                    try:
                        _data, address = sock.recvfrom(8192)
                    except socket.timeout:
                        break
                    ip = str(address[0])
                    if not self.guard.is_allowed_ip(ip):
                        self.logger.warning("DENIED_OUTSIDE_SCOPE ip=%s reason=mdns_response", ip[:64])
                        continue
                    assets.setdefault(ip, self._asset(ip, "mdns"))
        except (OSError, OutOfScopeTargetError) as exc:
            self.logger.debug("mdns discovery unavailable error=%s", type(exc).__name__)
        return list(assets.values())

    def discover(self) -> list[DiscoveredAsset]:
        if not self.settings.enabled:
            return []
        log_event(self.logger, "discovery_started", methods=list(self.settings.methods))
        assets: dict[str, DiscoveredAsset] = {}
        methods = tuple(method.casefold() for method in self.settings.methods)
        host_methods = tuple(method for method in methods if method in {"icmp", "arp", "tcp"})
        candidates = self._candidate_ips()
        denied_count = 0
        if host_methods and candidates:
            with ThreadPoolExecutor(max_workers=self.settings.max_concurrency, thread_name_prefix="cyber-discovery") as pool:
                futures = {pool.submit(self._host_probe, ip, host_methods): ip for ip in candidates}
                for future in as_completed(futures):
                    try:
                        asset = future.result()
                    except OutOfScopeTargetError:
                        denied_count += 1
                        continue
                    except Exception as exc:
                        self.logger.debug("discovery worker failed error=%s", type(exc).__name__)
                        continue
                    if asset is not None:
                        assets[asset.ip] = asset
        if "arp" in methods:
            for asset in self._discover_arp_cache():
                assets[asset.ip] = asset
        if "mdns" in methods:
            for asset in self._discover_mdns():
                assets[asset.ip] = asset
        if "ssdp" in methods:
            for asset in self._discover_ssdp():
                assets[asset.ip] = asset
        return list(assets.values())


__all__ = ["DiscoveryResult", "NetworkDiscovery", "RateLimiter"]
