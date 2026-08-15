"""Bounded local PTZ patrol policy for camera agents.

The policy deliberately has no camera-specific movement logic.  It decides
*when* to move; the ONVIF adapter decides *how* to move.  This keeps the
classifier and the physical actuator separate and makes every transition
deterministic enough to unit test.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import random
import threading
import time
from typing import Callable

from .ptz import PTZMove


class PatrolPhase(str, Enum):
    MANUAL = "manual"
    SEARCHING = "searching"
    OBSERVING = "observing"


class PatrolAction(str, Enum):
    MOVE_NEXT = "move_next"
    STOP = "stop"


@dataclass(frozen=True)
class BoundedSearchConfig:
    max_pan_steps: int = 3
    max_tilt_steps: int = 1


class BoundedRandomSearch:
    """Stochastic bounded search explorer for camera agents.

    Generates pseudo-random, non-predictable search moves within safe relative
    bounds (pan in [-max_pan, +max_pan], tilt in [-max_tilt, +max_tilt]). This
    prevents fixed-pattern anticipation ("bị bắt bài"), avoids blind-spots,
    and guarantees the camera never drifts off to physical limit stops.
    """

    def __init__(
        self,
        config: BoundedSearchConfig = BoundedSearchConfig(),
        *,
        rng: random.Random | None = None,
    ) -> None:
        self.config = config
        self._rng = rng if rng is not None else random.Random()
        self._pan = 0
        self._tilt = 0
        self._last_move: PTZMove | None = None
        self._lock = threading.Lock()

    @property
    def position(self) -> tuple[int, int]:
        with self._lock:
            return (self._pan, self._tilt)

    def reset(self) -> None:
        with self._lock:
            self._pan = 0
            self._tilt = 0
            self._last_move = None

    def next_direction(self) -> PTZMove:
        with self._lock:
            candidates: list[tuple[PTZMove, float]] = []
            max_pan = self.config.max_pan_steps
            max_tilt = self.config.max_tilt_steps

            # Horizontal candidates
            if self._pan < max_pan:
                weight = 2.0 if self._pan <= 0 else 1.0
                if self._last_move == PTZMove.RIGHT:
                    weight *= 1.5
                candidates.append((PTZMove.RIGHT, weight))
            if self._pan > -max_pan:
                weight = 2.0 if self._pan >= 0 else 1.0
                if self._last_move == PTZMove.LEFT:
                    weight *= 1.5
                candidates.append((PTZMove.LEFT, weight))

            # Vertical candidates
            if self._tilt < max_tilt:
                weight = 1.2 if self._tilt <= 0 else 0.6
                if self._last_move == PTZMove.UP:
                    weight *= 1.3
                candidates.append((PTZMove.UP, weight))
            if self._tilt > -max_tilt:
                weight = 1.2 if self._tilt >= 0 else 0.6
                if self._last_move == PTZMove.DOWN:
                    weight *= 1.3
                candidates.append((PTZMove.DOWN, weight))

            if not candidates:
                return PTZMove.RIGHT

            moves, weights = zip(*candidates)
            chosen = self._rng.choices(moves, weights=weights, k=1)[0]

            if chosen == PTZMove.RIGHT:
                self._pan += 1
            elif chosen == PTZMove.LEFT:
                self._pan -= 1
            elif chosen == PTZMove.UP:
                self._tilt += 1
            elif chosen == PTZMove.DOWN:
                self._tilt -= 1

            self._last_move = chosen
            return chosen


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
