from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
import logging
import os
from pathlib import Path
import threading
import time
from typing import Any

from ..config import PROJECT_DIR
from .config import CyberConfig
from .models import CameraEvent
from .orchestrator import CyberOrchestrator


LOGGER = logging.getLogger(__name__)


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


class CyberRuntimeBridge:
    """Optional asynchronous bridge from camera status events to cyber runs."""

    def __init__(self, orchestrator: CyberOrchestrator, *, cooldown_seconds: float = 30.0) -> None:
        self.orchestrator = orchestrator
        self.cooldown_seconds = max(1.0, cooldown_seconds)
        self._last_trigger: dict[str, float] = {}
        self._busy = False
        self._lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cyber-pipeline")
        self._future: Future[Any] | None = None

    @classmethod
    def from_environment(cls) -> "CyberRuntimeBridge | None":
        if not _env_bool("CYBER_AGENT_ENABLED", False):
            return None
        path = Path(os.environ.get("CYBER_AGENT_CONFIG", "config/cyber_agent.yaml"))
        if not path.is_absolute():
            path = PROJECT_DIR / path
        try:
            config = CyberConfig.from_yaml(path)
            if not config.enabled:
                return None
            return cls(
                CyberOrchestrator.from_config(
                    config,
                    dry_run=_env_bool("CYBER_AGENT_DRY_RUN", config.dry_run),
                ),
                cooldown_seconds=float(os.environ.get("CYBER_AGENT_COOLDOWN_SECONDS", "30")),
            )
        except (OSError, ValueError) as exc:
            LOGGER.error("Cyber bridge disabled after config error: %s", type(exc).__name__)
            return None

    @staticmethod
    def _device_class(snapshot: Any) -> str:
        configured = os.environ.get("CYBER_DEVICE_CLASS", "").strip().lower()
        if configured:
            return configured
        # Existing CV model is a screen detector, not an object-identity model.
        # Keep correlation explicitly unconfirmed instead of guessing laptop/router.
        return "unknown"

    def submit(self, snapshot: Any) -> bool:
        if not snapshot.camera_online or not getattr(snapshot.extraction, "computer_detected", False):
            return False
        now = time.monotonic()
        with self._lock:
            previous = self._last_trigger.get(snapshot.device_id, 0.0)
            if self._busy or now - previous < self.cooldown_seconds:
                return False
            self._last_trigger[snapshot.device_id] = now
            self._busy = True
        event = CameraEvent(
            agent_id=snapshot.device_id,
            camera_id=snapshot.device_id,
            device_class=self._device_class(snapshot),
            confidence=float(getattr(snapshot, "raw_active_score", 0.0)),
        )
        self._future = self._executor.submit(self._run, event)
        return True

    def _run(self, event: CameraEvent) -> None:
        try:
            report = self.orchestrator.process(event)
            LOGGER.info("Cyber pipeline completed state=%s events=%s errors=%s", report.state, len(report.events), len(report.errors))
        except Exception as exc:
            LOGGER.error("Cyber pipeline failed closed error=%s", type(exc).__name__)
        finally:
            with self._lock:
                self._busy = False

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)


__all__ = ["CyberRuntimeBridge"]
