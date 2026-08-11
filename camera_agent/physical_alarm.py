from __future__ import annotations

from dataclasses import dataclass, field
import importlib
import logging
import threading
import time
from typing import Callable


LOGGER = logging.getLogger(__name__)


class PhysicalAlarmError(RuntimeError):
    pass


class PhysicalAlarm:
    def trigger(self, reason: str) -> None:
        raise NotImplementedError


class NoopPhysicalAlarm(PhysicalAlarm):
    def trigger(self, reason: str) -> None:
        return None


@dataclass
class TapoSirenAlarm(PhysicalAlarm):
    host: str
    username: str = field(repr=False)
    password: str = field(repr=False)
    duration_seconds: float = 3.0
    cooldown_seconds: float = 30.0
    clock: Callable[[], float] = time.monotonic
    _last_trigger_at: float | None = field(default=None, init=False, repr=False)
    _in_flight: bool = field(default=False, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.duration_seconds <= 0:
            raise PhysicalAlarmError("Tapo alarm duration must be greater than zero")
        if self.cooldown_seconds < self.duration_seconds:
            raise PhysicalAlarmError(
                "Tapo alarm cooldown must be at least the alarm duration"
            )

    def trigger(self, reason: str) -> None:
        now = self.clock()
        with self._lock:
            if self._in_flight:
                LOGGER.info("Tapo physical alarm already running; skipped duplicate trigger")
                return
            if (
                self._last_trigger_at is not None
                and now - self._last_trigger_at < self.cooldown_seconds
            ):
                LOGGER.info("Tapo physical alarm is in cooldown; skipped trigger")
                return
            self._in_flight = True
            self._last_trigger_at = now

        thread = threading.Thread(
            target=self._run_alarm,
            args=(reason,),
            name="tapo-physical-alarm",
            daemon=True,
        )
        thread.start()

    def _run_alarm(self, reason: str) -> None:
        try:
            module = importlib.import_module("pytapo")
            tapo_class = getattr(module, "Tapo")
        except (ImportError, AttributeError):
            LOGGER.error(
                "Tapo physical alarm needs optional package 'pytapo'. "
                "Install it before enabling physical_alarm."
            )
            self._mark_finished()
            return

        camera = None
        try:
            camera = tapo_class(
                self.host,
                self.username,
                self.password,
                printDebugInformation=False,
                printWarnInformation=False,
            )
            LOGGER.warning("Tapo physical alarm ON: %s", reason)
            camera.setSirenStatus(True)
            time.sleep(self.duration_seconds)
            camera.setSirenStatus(False)
            LOGGER.warning("Tapo physical alarm OFF")
        except Exception as exc:
            LOGGER.error("Tapo physical alarm failed: %s", exc)
            try:
                if camera is not None:
                    camera.setSirenStatus(False)
            except Exception:
                pass
        finally:
            self._mark_finished()

    def _mark_finished(self) -> None:
        with self._lock:
            self._in_flight = False


def build_tapo_alarm(
    *,
    enabled: bool,
    host: str | None,
    username: str | None,
    password: str | None,
    duration_seconds: float,
    cooldown_seconds: float,
) -> PhysicalAlarm:
    if not enabled:
        return NoopPhysicalAlarm()
    if not host or not username or not password:
        raise PhysicalAlarmError(
            "Tapo physical alarm requires structured host, username and password"
        )
    return TapoSirenAlarm(
        host=host,
        username=username,
        password=password,
        duration_seconds=duration_seconds,
        cooldown_seconds=cooldown_seconds,
    )
