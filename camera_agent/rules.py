from __future__ import annotations

from dataclasses import dataclass, replace
from enum import IntEnum
from pathlib import Path
import time
import tomllib
from typing import Any

from .decision import AgentState, Decision


class RuleConfigurationError(ValueError):
    pass


class RuleStatus(IntEnum):
    NORMAL = 0
    PENDING = 1
    VIOLATION = 2
    COOLDOWN = 3


@dataclass(frozen=True)
class FacebookRuleConfig:
    enabled: bool = True
    minimum_confidence: float = 0.72
    trigger_after_seconds: float = 3.0
    cooldown_seconds: float = 60.0


@dataclass(frozen=True)
class CameraOfflineRuleConfig:
    enabled: bool = True
    trigger_after_seconds: float = 0.0


@dataclass(frozen=True)
class RulesConfig:
    facebook: FacebookRuleConfig
    camera_offline: CameraOfflineRuleConfig

    @classmethod
    def from_toml(cls, path: Path) -> "RulesConfig":
        try:
            with path.open("rb") as handle:
                payload = tomllib.load(handle)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise RuleConfigurationError(f"Could not read rules file {path}: {exc}") from exc

        facebook = payload.get("facebook_usage", {})
        offline = payload.get("camera_offline", {})
        config = cls(
            facebook=FacebookRuleConfig(
                enabled=bool(facebook.get("enabled", True)),
                minimum_confidence=float(facebook.get("minimum_confidence", 0.72)),
                trigger_after_seconds=float(facebook.get("trigger_after_seconds", 3.0)),
                cooldown_seconds=float(facebook.get("cooldown_seconds", 60.0)),
            ),
            camera_offline=CameraOfflineRuleConfig(
                enabled=bool(offline.get("enabled", True)),
                trigger_after_seconds=float(offline.get("trigger_after_seconds", 0.0)),
            ),
        )
        config.validate()
        return config

    def validate(self) -> None:
        errors: list[str] = []
        if not 0 <= self.facebook.minimum_confidence <= 1:
            errors.append("facebook_usage.minimum_confidence must be between 0 and 1")
        if self.facebook.trigger_after_seconds < 0:
            errors.append("facebook_usage.trigger_after_seconds must be >= 0")
        if self.facebook.cooldown_seconds < 1:
            errors.append("facebook_usage.cooldown_seconds must be >= 1")
        if self.camera_offline.trigger_after_seconds < 0:
            errors.append("camera_offline.trigger_after_seconds must be >= 0")
        if errors:
            raise RuleConfigurationError("Invalid rules:\n- " + "\n- ".join(errors))

    def with_overrides(self, overrides: dict[str, Any] | None) -> "RulesConfig":
        """Return a validated per-device copy without mutating the shared defaults."""
        if not overrides:
            return self
        if not isinstance(overrides, dict):
            raise RuleConfigurationError("rules.overrides must be an object")
        known_sections = {"facebook_usage", "camera_offline"}
        unknown_sections = sorted(set(overrides) - known_sections)
        if unknown_sections:
            raise RuleConfigurationError(
                f"unknown rule section: {unknown_sections[0]}"
            )

        facebook = self.facebook
        raw_facebook = overrides.get("facebook_usage", {})
        if not isinstance(raw_facebook, dict):
            raise RuleConfigurationError("facebook_usage override must be an object")
        facebook_fields = {
            "enabled",
            "minimum_confidence",
            "trigger_after_seconds",
            "cooldown_seconds",
        }
        unknown_facebook = sorted(set(raw_facebook) - facebook_fields)
        if unknown_facebook:
            raise RuleConfigurationError(
                f"unknown rule override: facebook_usage.{unknown_facebook[0]}"
            )
        if raw_facebook:
            facebook = replace(
                facebook,
                **{
                    name: _rule_value(
                        raw_facebook[name],
                        boolean=name == "enabled",
                        field=f"facebook_usage.{name}",
                    )
                    for name in raw_facebook
                },
            )

        camera_offline = self.camera_offline
        raw_offline = overrides.get("camera_offline", {})
        if not isinstance(raw_offline, dict):
            raise RuleConfigurationError("camera_offline override must be an object")
        offline_fields = {"enabled", "trigger_after_seconds"}
        unknown_offline = sorted(set(raw_offline) - offline_fields)
        if unknown_offline:
            raise RuleConfigurationError(
                f"unknown rule override: camera_offline.{unknown_offline[0]}"
            )
        if raw_offline:
            camera_offline = replace(
                camera_offline,
                **{
                    name: _rule_value(
                        raw_offline[name],
                        boolean=name == "enabled",
                        field=f"camera_offline.{name}",
                    )
                    for name in raw_offline
                },
            )

        effective = replace(self, facebook=facebook, camera_offline=camera_offline)
        effective.validate()
        return effective


def _rule_value(value: Any, *, boolean: bool, field: str) -> bool | float:
    if boolean:
        if not isinstance(value, bool):
            raise RuleConfigurationError(f"{field} must be true or false")
        return value
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RuleConfigurationError(f"{field} must be a number")
    return float(value)


@dataclass(frozen=True)
class RuleEvaluation:
    status: RuleStatus = RuleStatus.NORMAL
    event_triggered: bool = False
    event_cleared: bool = False
    active_seconds: float = 0.0
    violation_count: int = 0
    camera_offline_event: bool = False
    camera_recovered_event: bool = False
    camera_offline_seconds: float = 0.0


class MonitoringRuleEngine:
    """Turn stable AI decisions into auditable Mainflux rule events."""

    def __init__(self, config: RulesConfig, *, clock=time.monotonic) -> None:
        self.config = config
        self.clock = clock
        self._facebook_since: float | None = None
        self._last_violation_at: float | None = None
        self._violation_open = False
        self._violation_count = 0
        self._offline_since: float | None = None
        self._offline_event_sent = False

    def update(self, decision: Decision) -> RuleEvaluation:
        now = self.clock()
        camera_offline_event = False
        camera_recovered_event = False
        offline_seconds = 0.0

        if decision.state == AgentState.CAMERA_OFFLINE:
            if self._offline_since is None:
                self._offline_since = now
                self._offline_event_sent = False
            offline_seconds = now - self._offline_since
            if (
                self.config.camera_offline.enabled
                and not self._offline_event_sent
                and offline_seconds >= self.config.camera_offline.trigger_after_seconds
            ):
                camera_offline_event = True
                self._offline_event_sent = True
        elif self._offline_since is not None:
            camera_recovered_event = self._offline_event_sent
            self._offline_since = None
            self._offline_event_sent = False

        # DecisionEngine has already applied the activation/deactivation
        # thresholds before exposing FACEBOOK_DETECTED. Use that state as the
        # single alarm gate so a valid detection cannot be suppressed by a
        # second, drifting confidence check in the rule layer.
        facebook_qualified = bool(
            self.config.facebook.enabled
            and decision.state == AgentState.FACEBOOK_DETECTED
        )
        if not facebook_qualified:
            cleared = self._violation_open
            self._facebook_since = None
            self._violation_open = False
            return RuleEvaluation(
                status=RuleStatus.NORMAL,
                event_cleared=cleared,
                violation_count=self._violation_count,
                camera_offline_event=camera_offline_event,
                camera_recovered_event=camera_recovered_event,
                camera_offline_seconds=offline_seconds,
            )

        if self._facebook_since is None:
            self._facebook_since = now
        active_seconds = now - self._facebook_since
        if active_seconds < self.config.facebook.trigger_after_seconds:
            status = RuleStatus.PENDING
            triggered = False
        else:
            cooldown_over = bool(
                self._last_violation_at is None
                or now - self._last_violation_at >= self.config.facebook.cooldown_seconds
            )
            if cooldown_over:
                status = RuleStatus.VIOLATION
                triggered = True
                self._last_violation_at = now
                self._violation_open = True
                self._violation_count += 1
            else:
                status = RuleStatus.COOLDOWN
                triggered = False

        return RuleEvaluation(
            status=status,
            event_triggered=triggered,
            active_seconds=active_seconds,
            violation_count=self._violation_count,
            camera_offline_event=camera_offline_event,
            camera_recovered_event=camera_recovered_event,
            camera_offline_seconds=offline_seconds,
        )
