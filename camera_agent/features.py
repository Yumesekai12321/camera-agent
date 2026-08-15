"""Exclusive, locally installed feature selection for camera agents.

The control plane is allowed to select a feature that already ships with the
agent.  It is intentionally *not* a plugin runner: no code, URL or model may
be supplied in a control message.  This gives Mainflux a useful desired-state
role without turning the camera fleet into a remote-code-execution surface.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import threading


class FeatureError(ValueError):
    """Raised when a requested feature state is not safe to apply."""


class FeatureId(str, Enum):
    FACEBOOK_MONITOR = "facebook_monitor"
    PERSON_GUARD = "person_guard"
    NONE = "none"


FEATURE_CODES: dict[FeatureId, int] = {
    FeatureId.NONE: 0,
    FeatureId.FACEBOOK_MONITOR: 1,
    FeatureId.PERSON_GUARD: 2,
}


@dataclass(frozen=True)
class DesiredFeatureState:
    """The one feature and runtime state selected for a device.

    ``generation`` is owned by the central control hub.  Agents only move
    forward through generations, making delayed MQTT deliveries harmless.
    """

    runtime_enabled: bool
    feature: FeatureId
    generation: int = 0

    @property
    def active_feature(self) -> FeatureId:
        return self.feature if self.runtime_enabled else FeatureId.NONE


class FeatureRuntime:
    """Thread-safe desired-state holder shared by command and inference loops."""

    def __init__(
        self,
        *,
        allowed: set[FeatureId] | frozenset[FeatureId] | tuple[FeatureId, ...],
        initial_feature: FeatureId = FeatureId.FACEBOOK_MONITOR,
        runtime_enabled: bool = True,
    ) -> None:
        allowed_set = frozenset(allowed)
        if not allowed_set:
            raise FeatureError("at least one installed feature is required")
        if FeatureId.NONE in allowed_set:
            raise FeatureError("none is a state, not an installable feature")
        if initial_feature == FeatureId.NONE or initial_feature not in allowed_set:
            raise FeatureError("initial feature must be an installed feature")
        self._allowed = allowed_set
        self._state = DesiredFeatureState(
            runtime_enabled=bool(runtime_enabled), feature=initial_feature, generation=0
        )
        self._lock = threading.Lock()

    @property
    def allowed(self) -> frozenset[FeatureId]:
        return self._allowed

    def snapshot(self) -> DesiredFeatureState:
        with self._lock:
            return self._state

    def apply(self, state: DesiredFeatureState) -> tuple[DesiredFeatureState, bool]:
        """Apply one monotonic desired state and report whether it changed.

        A duplicate generation with the same data is an idempotent retry.  A
        duplicate generation with different data is rejected instead of letting
        two control paths race to own one physical camera.
        """

        if state.generation < 0:
            raise FeatureError("control generation must be non-negative")
        if state.feature != FeatureId.NONE and state.feature not in self._allowed:
            raise FeatureError("feature is not installed for this device")
        with self._lock:
            current = self._state
            if state.generation < current.generation:
                return current, False
            if state.generation == current.generation:
                if (
                    state.runtime_enabled != current.runtime_enabled
                    or state.feature != current.feature
                ):
                    raise FeatureError("conflicting control generation")
                return current, False
            self._state = DesiredFeatureState(
                runtime_enabled=bool(state.runtime_enabled),
                feature=state.feature,
                generation=state.generation,
            )
            return self._state, True
