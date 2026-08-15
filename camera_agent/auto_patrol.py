"""Bounded local PTZ patrol policy for camera agents.

The policy deliberately has no camera-specific movement logic.  It decides
*when* to move; the ONVIF adapter decides *how* to move.  This keeps the
classifier and the physical actuator separate and makes every transition
deterministic enough to unit test.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import threading
import time
from typing import Callable


class PatrolPhase(str, Enum):
    MANUAL = "manual"
    SEARCHING = "searching"
    OBSERVING = "observing"


class PatrolAction(str, Enum):
    MOVE_NEXT = "move_next"
    STOP = "stop"


@dataclass(frozen=True)
class AutoPatrolConfig:
    enabled: bool = False
    observe_seconds: float = 5.0
    search_move_interval_seconds: float = 1.0
    max_alarm_events_per_screen: int = 3

    def validate(self) -> None:
        if not 1 <= self.observe_seconds <= 120:
            raise ValueError("observe_seconds must be between 1 and 120")
        if not 0.1 <= self.search_move_interval_seconds <= 30:
            raise ValueError("search_move_interval_seconds must be between 0.1 and 30")
        if not 1 <= self.max_alarm_events_per_screen <= 10:
            raise ValueError("max_alarm_events_per_screen must be between 1 and 10")


class AutoPatrol:
    """State machine for a local auto/manual switch.

    A new screen starts a finite observation window.  If Facebook never
    produces a local rule event during that window, the patrol moves on.  If
    Facebook persists, three independent, cooldown-governed rule events end
    the window early after the third alarm.
    """

    def __init__(
        self,
        config: AutoPatrolConfig,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        config.validate()
        self.config = config
        self.clock = clock
        self._enabled = config.enabled
        self._phase = PatrolPhase.SEARCHING if config.enabled else PatrolPhase.MANUAL
        self._observing_since: float | None = None
        self._next_search_move_at = 0.0
        self._alarm_events = 0
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        with self._lock:
            return self._enabled

    @property
    def phase(self) -> PatrolPhase:
        with self._lock:
            return self._phase

    @property
    def alarm_events(self) -> int:
        with self._lock:
            return self._alarm_events

    def set_enabled(self, enabled: bool) -> None:
        with self._lock:
            self._enabled = bool(enabled)
            self._phase = PatrolPhase.SEARCHING if self._enabled else PatrolPhase.MANUAL
            self._observing_since = None
            self._next_search_move_at = 0.0
            self._alarm_events = 0

    def observe(self, *, screen_detected: bool, alarm_event: bool) -> tuple[PatrolAction, ...]:
        now = self.clock()
        with self._lock:
            if not self._enabled:
                return ()

            if self._phase == PatrolPhase.SEARCHING:
                if screen_detected:
                    self._phase = PatrolPhase.OBSERVING
                    self._observing_since = now
                    self._alarm_events = 0
                    return (PatrolAction.STOP,)
                if now >= self._next_search_move_at:
                    self._next_search_move_at = now + self.config.search_move_interval_seconds
                    return (PatrolAction.MOVE_NEXT,)
                return ()

            if alarm_event:
                self._alarm_events += 1
                if self._alarm_events >= self.config.max_alarm_events_per_screen:
                    return self._resume_search_locked(now)

            if (
                self._observing_since is not None
                and now - self._observing_since >= self.config.observe_seconds
                and self._alarm_events == 0
            ):
                return self._resume_search_locked(now)
            return ()

    def _resume_search_locked(self, now: float) -> tuple[PatrolAction, ...]:
        self._phase = PatrolPhase.SEARCHING
        self._observing_since = None
        self._alarm_events = 0
        self._next_search_move_at = now + self.config.search_move_interval_seconds
        return (PatrolAction.MOVE_NEXT,)
