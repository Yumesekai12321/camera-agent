from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from enum import IntEnum


class AgentState(IntEnum):
    NO_COMPUTER = 0
    COMPUTER_NO_FACEBOOK = 1
    FACEBOOK_DETECTED = 2
    CAMERA_OFFLINE = 3


@dataclass(frozen=True)
class Decision:
    state: AgentState
    computer_detected: bool
    facebook_active: bool
    active_score: float
    votes: int
    samples: int


class DecisionEngine:
    """Temporal voting with separate activation/deactivation thresholds."""

    def __init__(
        self,
        *,
        history_size: int = 8,
        minimum_history: int = 6,
        minimum_active_frames: int = 5,
        active_frame_threshold: float = 0.55,
        activate_average: float = 0.72,
        deactivate_average: float = 0.35,
    ) -> None:
        self.history: deque[float] = deque(maxlen=history_size)
        self.minimum_history = minimum_history
        self.minimum_active_frames = minimum_active_frames
        self.active_frame_threshold = active_frame_threshold
        self.activate_average = activate_average
        self.deactivate_average = deactivate_average
        self.facebook_active = False

    def reset(self) -> None:
        self.history.clear()
        self.facebook_active = False

    def camera_offline(self) -> Decision:
        self.reset()
        return Decision(AgentState.CAMERA_OFFLINE, False, False, 0.0, 0, 0)

    def update(self, *, computer_detected: bool, active_score: float = 0.0) -> Decision:
        if not computer_detected:
            self.reset()
            return Decision(AgentState.NO_COMPUTER, False, False, 0.0, 0, 0)

        score = min(1.0, max(0.0, float(active_score)))
        self.history.append(score)
        average = sum(self.history) / len(self.history)
        votes = sum(item >= self.active_frame_threshold for item in self.history)
        enough_history = len(self.history) >= self.minimum_history

        if not self.facebook_active:
            self.facebook_active = bool(
                enough_history
                and votes >= self.minimum_active_frames
                and average >= self.activate_average
            )
        elif enough_history and average < self.deactivate_average:
            self.facebook_active = False

        state = (
            AgentState.FACEBOOK_DETECTED
            if self.facebook_active
            else AgentState.COMPUTER_NO_FACEBOOK
        )
        return Decision(
            state,
            True,
            self.facebook_active,
            average,
            votes,
            len(self.history),
        )

