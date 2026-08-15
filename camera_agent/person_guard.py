"""Anonymous person presence, alerting and bounded PTZ following."""

from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Callable

from .ptz import PTZMove
from .vision import PersonDetection


@dataclass(frozen=True)
class PersonGuardConfig:
    minimum_confidence: float = 0.45
    confirmation_frames: int = 2
    absence_rearm_seconds: float = 3.0
    tracking_dead_zone: float = 0.15
    tracking_move_duration_seconds: float = 0.25
    tracking_move_interval_seconds: float = 0.5

    def validate(self) -> None:
        if not 0 <= self.minimum_confidence <= 1:
            raise ValueError("person minimum confidence must be between 0 and 1")
        if not 1 <= self.confirmation_frames <= 10:
            raise ValueError("person confirmation frames must be between 1 and 10")
        if not 0.1 <= self.absence_rearm_seconds <= 120:
            raise ValueError("person absence rearm seconds must be between 0.1 and 120")
        if not 0 < self.tracking_dead_zone < 0.5:
            raise ValueError("person tracking dead zone must be between 0 and 0.5")
        if not 0.1 <= self.tracking_move_duration_seconds <= 0.25:
            raise ValueError("person tracking move duration must be between 0.1 and 0.25")
        if not 0.1 <= self.tracking_move_interval_seconds <= 30:
            raise ValueError("person tracking move interval must be between 0.1 and 30")


@dataclass(frozen=True)
class PersonEvaluation:
    present: bool = False
    confidence: float = 0.0
    alert_event: bool = False
    event_cleared: bool = False
    consecutive_frames: int = 0


class PersonGuard:
    """Confirm a generic person class without retaining a cross-frame identity."""

    def __init__(
        self,
        config: PersonGuardConfig = PersonGuardConfig(),
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        config.validate()
        self.config = config
        self.clock = clock
        self._consecutive = 0
        self._present = False
        self._absent_since: float | None = None
        self._last_track_at = float("-inf")

    def reset(self) -> None:
        self._consecutive = 0
        self._present = False
        self._absent_since = None
        self._last_track_at = float("-inf")

    def observe(self, detection: PersonDetection | None) -> PersonEvaluation:
        now = self.clock()
        qualified = bool(
            detection is not None and detection.confidence >= self.config.minimum_confidence
        )
        if qualified:
            self._consecutive += 1
            self._absent_since = None
            became_present = not self._present and self._consecutive >= self.config.confirmation_frames
            if became_present:
                self._present = True
            return PersonEvaluation(
                present=self._present,
                confidence=float(detection.confidence),
                alert_event=became_present,
                consecutive_frames=self._consecutive,
            )

        self._consecutive = 0
        if not self._present:
            return PersonEvaluation()
        if self._absent_since is None:
            self._absent_since = now
        if now - self._absent_since < self.config.absence_rearm_seconds:
            return PersonEvaluation(present=True)
        self._present = False
        self._absent_since = None
        return PersonEvaluation(event_cleared=True)

    def next_tracking_move(
        self,
        detection: PersonDetection | None,
        *,
        auto_enabled: bool,
    ) -> PTZMove | None:
        """Return at most one short corrective move for the confirmed target."""

        if not auto_enabled or not self._present or detection is None:
            return None
        now = self.clock()
        if now - self._last_track_at < self.config.tracking_move_interval_seconds:
            return None
        delta_x = detection.center_x - 0.5
        delta_y = detection.center_y - 0.5
        if max(abs(delta_x), abs(delta_y)) <= self.config.tracking_dead_zone:
            return None
        self._last_track_at = now
        if abs(delta_x) >= abs(delta_y):
            return PTZMove.RIGHT if delta_x > 0 else PTZMove.LEFT
        return PTZMove.DOWN if delta_y > 0 else PTZMove.UP
