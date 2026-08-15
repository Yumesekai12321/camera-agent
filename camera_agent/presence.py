"""Short-lived local heartbeat used by the loopback control plane."""

from __future__ import annotations

import os
from pathlib import Path
import re
import threading
import time

from .config import PROJECT_DIR


_DEVICE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")


class AgentPresence:
    """One timestamp per device; stale files never imply a live agent."""

    def __init__(self, root: Path | None = None, *, minimum_interval_seconds: float = 1.0) -> None:
        if minimum_interval_seconds < 0:
            raise ValueError("minimum_interval_seconds must not be negative")
        self.root = (root or PROJECT_DIR / "runtime" / "agent-presence").resolve()
        self.minimum_interval_seconds = minimum_interval_seconds
        self._last_touch = 0.0
        self._lock = threading.Lock()

    def path_for(self, device_id: str) -> Path:
        if not isinstance(device_id, str) or not _DEVICE_ID.fullmatch(device_id):
            raise ValueError("invalid device id for presence")
        return self.root / f"{device_id}.heartbeat"

    def touch(self, device_id: str, *, now: float | None = None) -> bool:
        current = time.time() if now is None else now
        monotonic_now = time.monotonic()
        with self._lock:
            if monotonic_now - self._last_touch < self.minimum_interval_seconds:
                return False
            self._last_touch = monotonic_now
        path = self.path_for(device_id)
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_text(f"{current:.6f}\n", encoding="ascii")
        os.replace(temporary, path)
        return True

    def clear(self, device_id: str) -> None:
        path = self.path_for(device_id)
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        for temporary in path.parent.glob(f".{path.name}.*.tmp"):
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def is_online(self, device_id: str, *, max_age_seconds: float = 4.0) -> bool:
        if max_age_seconds <= 0:
            raise ValueError("max_age_seconds must be positive")
        try:
            age = time.time() - self.path_for(device_id).stat().st_mtime
        except (FileNotFoundError, ValueError, OSError):
            return False
        return 0 <= age <= max_age_seconds
