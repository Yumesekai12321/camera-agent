from __future__ import annotations

from datetime import datetime, timedelta
from dataclasses import dataclass, replace
from itertools import cycle
import logging
import threading
import time
from typing import Callable

import cv2
import numpy as np

from .camera import FrameSource, RTSPCamera
from .config import Settings
from .decision import AgentState, Decision, DecisionEngine
from .mainflux import MainfluxError, MainfluxPublisher, Telemetry
from .physical_alarm import NoopPhysicalAlarm, PhysicalAlarm
from .auto_patrol import AutoPatrol, AutoPatrolConfig, BoundedRandomSearch, PatrolAction
from .control import CommandStore
from .features import DesiredFeatureState, FeatureError, FeatureId, FeatureRuntime
from .local_preview import LocalPreviewStore
from .person_guard import PersonEvaluation, PersonGuard
from .presence import AgentPresence
from .ptz import NoopPTZ, PTZArbiter, PTZController, PTZError, PTZMove
from .rules import MonitoringRuleEngine, RuleEvaluation, RulesConfig, RuleStatus
from .vision import (
    ComputerScreenDetector,
    Detection,
    FacebookClassifier,
    PersonDetector,
    ScreenExtraction,
    screen_centering_move,
)


LOGGER = logging.getLogger(__name__)
FULL_WINDOW = "Camera Agent - FULL CAMERA FRAME"
COMPACT_WINDOW = "Camera Agent - COMPACT STATUS"
CLASSIFIER_WINDOW = "Camera Agent - Classifier input"


@dataclass(frozen=True)
class AgentSnapshot:
    device_id: str
    device_name: str
    extraction: ScreenExtraction
    decision: Decision | None
    rule: RuleEvaluation
    raw_active_score: float
    classifier_margin: float
    camera_online: bool
    updated_at: float
    active_feature: FeatureId = FeatureId.FACEBOOK_MONITOR
    runtime_enabled: bool = True
    person_present: bool = False
    person_tracking: bool = False
    control_generation: int = 0


def resize_preview(
    image: np.ndarray,
    max_width: int = 1280,
    max_height: int = 720,
) -> np.ndarray:
    """Fit the entire image inside the preview bounds without cropping it."""
    height, width = image.shape[:2]
    scale = min(max_width / width, max_height / height, 1.0)
    if scale >= 1:
        return image.copy()
    return cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)


def _draw_full_frame_corners(image: np.ndarray) -> None:
    height, width = image.shape[:2]
    length = max(20, min(width, height) // 25)
    color = (255, 255, 0)
    thickness = 3
    for x, y, sx, sy in (
        (0, 0, 1, 1),
        (width - 1, 0, -1, 1),
        (width - 1, height - 1, -1, -1),
        (0, height - 1, 1, -1),
    ):
        cv2.line(image, (x, y), (x + sx * length, y), color, thickness)
        cv2.line(image, (x, y), (x, y + sy * length), color, thickness)


def annotate_full_frame(
    frame: np.ndarray,
    extraction: ScreenExtraction,
    decision: Decision,
    rule: RuleEvaluation,
    raw_active_score: float,
    classifier_margin: float,
) -> np.ndarray:
    output = frame.copy()
    _draw_full_frame_corners(output)
    if extraction.detection:
        x1, y1, x2, y2 = extraction.detection.box
        cv2.rectangle(output, (x1, y1), (x2, y2), (0, 210, 0), 2)
        label = "MONITOR ROI" if extraction.used_static_roi else (
            f"COMPUTER {extraction.detection.confidence:.2f}"
        )
        cv2.putText(
            output,
            label,
            (x1, max(25, y1 - 8)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.65,
            (0, 255, 0),
            2,
        )

    color = (0, 0, 255) if decision.facebook_active else (0, 220, 0)
    source_height, source_width = frame.shape[:2]
    lines = [
        f"FULL FRAME {source_width}x{source_height}",
        decision.state.name,
        (
            f"raw={raw_active_score:.3f} avg={decision.active_score:.3f} "
            f"margin={classifier_margin:.3f}"
        ),
        f"votes={decision.votes}/{decision.samples} rule={rule.status.name}",
        "V=compact/full  Q=quit",
    ]
    for index, text in enumerate(lines):
        cv2.putText(
            output,
            text,
            (20, 38 + index * 30),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.68,
            color if index == 1 else (255, 255, 255),
            2,
        )
    return output


def render_compact_status(
    extraction: ScreenExtraction,
    decision: Decision,
    rule: RuleEvaluation,
    raw_active_score: float,
    classifier_margin: float,
    *,
    camera_online: bool,
) -> np.ndarray:
    """Render status without camera pixels, preventing recursive self-preview."""
    panel = np.full((230, 540, 3), (24, 24, 28), dtype=np.uint8)
    alert = decision.facebook_active or rule.event_triggered
    accent = (
        (0, 0, 235)
        if alert
        else ((0, 200, 0) if camera_online else (0, 180, 255))
    )
    cv2.rectangle(panel, (0, 0), (539, 229), accent, 5)
    monitor_found = extraction.computer_detected and extraction.screen is not None
    status = "ALERT" if alert else ("READY" if camera_online else "OFFLINE")
    lines = [
        (status, 1.0, accent),
        (
            f"CAMERA {'ONLINE' if camera_online else 'OFFLINE'}   "
            f"SCREEN {'YES' if monitor_found else 'NO'}",
            0.62,
            (235, 235, 235),
        ),
        (
            f"AI {decision.state.name}   RULE {rule.status.name}",
            0.58,
            (235, 235, 235),
        ),
        (
            f"raw {raw_active_score:.3f}   avg {decision.active_score:.3f}   "
            f"margin {classifier_margin:.3f}",
            0.58,
            (190, 190, 190),
        ),
        ("V = full/compact     Q = quit", 0.55, (150, 150, 150)),
    ]
    for index, (text, scale, color) in enumerate(lines):
        cv2.putText(
            panel,
            text,
            (20, 45 + index * 40),
            cv2.FONT_HERSHEY_SIMPLEX,
            scale,
            color,
            2,
        )
    return panel


def render_person_guard_status(
    evaluation: PersonEvaluation,
    *,
    camera_online: bool,
    tracking: bool,
) -> np.ndarray:
    """Compact Person Guard status without camera pixels."""

    panel = np.full((230, 540, 3), (24, 24, 28), dtype=np.uint8)
    accent = (0, 0, 235) if evaluation.present else ((0, 200, 0) if camera_online else (0, 180, 255))
    cv2.rectangle(panel, (0, 0), (539, 229), accent, 5)
    lines = [
        ("PERSON GUARD", 0.92, accent),
        (f"CAMERA {'ONLINE' if camera_online else 'OFFLINE'}   PERSON {'YES' if evaluation.present else 'NO'}", 0.58, (235, 235, 235)),
        (f"confidence {evaluation.confidence:.3f}   confirm {evaluation.consecutive_frames}", 0.58, (190, 190, 190)),
        (f"AUTO TRACK {'ON' if tracking else 'OFF'}", 0.62, (235, 235, 235)),
        ("V = full/compact     Q = quit", 0.55, (150, 150, 150)),
    ]
    for index, (text, scale, color) in enumerate(lines):
        cv2.putText(panel, text, (20, 45 + index * 40), cv2.FONT_HERSHEY_SIMPLEX, scale, color, 2)
    return panel


class PreviewController:
    def __init__(self, settings: Settings) -> None:
        self.enabled = settings.preview
        self.mode = settings.preview_mode if settings.preview else "off"
        self.max_width = settings.preview_max_width
        self.max_height = settings.preview_max_height
        self.show_classifier = settings.show_classifier_preview
        self._window_mode: str | None = None

    @property
    def self_preview_suppressed(self) -> bool:
        return self.mode in {"compact", "off"}

    def _prepare_window(self) -> None:
        if not self.enabled or self.mode == "off" or self._window_mode == self.mode:
            return
        cv2.destroyAllWindows()
        window = FULL_WINDOW if self.mode == "full" else COMPACT_WINDOW
        cv2.namedWindow(window, cv2.WINDOW_NORMAL)
        if self.mode == "compact":
            cv2.resizeWindow(window, 540, 230)
        self._window_mode = self.mode

    def show(
        self,
        *,
        frame: np.ndarray | None,
        extraction: ScreenExtraction,
        decision: Decision,
        rule: RuleEvaluation,
        raw_active_score: float,
        classifier_margin: float,
        camera_online: bool,
    ) -> None:
        if not self.enabled or self.mode == "off":
            return
        self._prepare_window()
        if self.mode == "full" and frame is not None:
            image = annotate_full_frame(
                frame,
                extraction,
                decision,
                rule,
                raw_active_score,
                classifier_margin,
            )
            cv2.imshow(
                FULL_WINDOW,
                resize_preview(image, self.max_width, self.max_height),
            )
            if self.show_classifier and extraction.screen is not None:
                cv2.imshow(
                    CLASSIFIER_WINDOW,
                    resize_preview(extraction.screen, 800, 500),
                )
        else:
            cv2.imshow(
                COMPACT_WINDOW,
                render_compact_status(
                    extraction,
                    decision,
                    rule,
                    raw_active_score,
                    classifier_margin,
                    camera_online=camera_online,
                ),
            )

    def show_person_guard(
        self,
        *,
        frame: np.ndarray | None,
        evaluation: PersonEvaluation,
        camera_online: bool,
        tracking: bool,
    ) -> None:
        if not self.enabled or self.mode == "off":
            return
        self._prepare_window()
        if self.mode == "full" and frame is not None:
            image = frame.copy()
            _draw_full_frame_corners(image)
            cv2.putText(
                image,
                f"PERSON GUARD {'TRACKING' if tracking else 'READY'} {evaluation.confidence:.3f}",
                (20, 38), cv2.FONT_HERSHEY_SIMPLEX, 0.68,
                (0, 0, 255) if evaluation.present else (0, 220, 0), 2,
            )
            cv2.imshow(FULL_WINDOW, resize_preview(image, self.max_width, self.max_height))
        else:
            cv2.imshow(
                COMPACT_WINDOW,
                render_person_guard_status(evaluation, camera_online=camera_online, tracking=tracking),
            )

    def poll(self) -> bool:
        if not self.enabled or self.mode == "off":
            return False
        key = cv2.waitKey(1) & 0xFF
        if key == ord("q"):
            return True
        if key == ord("v"):
            self.mode = "compact" if self.mode == "full" else "full"
            self._window_mode = None
        return False


class CameraAgent:
    def __init__(
        self,
        settings: Settings,
        *,
        dry_run: bool = False,
        device_id: str = "default",
        device_name: str = "Camera",
        detector: ComputerScreenDetector | None = None,
        classifier: FacebookClassifier | None = None,
        camera: FrameSource | None = None,
        rules_config: RulesConfig | None = None,
        physical_alarm: PhysicalAlarm | None = None,
        ptz: PTZController | None = None,
        auto_patrol: AutoPatrol | None = None,
        feature_runtime: FeatureRuntime | None = None,
        person_detector: PersonDetector | None = None,
        person_guard: PersonGuard | None = None,
        search_explorer: BoundedRandomSearch | None = None,
        command_store: CommandStore | None = None,
        local_preview: LocalPreviewStore | None = None,
        presence: AgentPresence | None = None,
        control_ack_callback: Callable[[str, DesiredFeatureState, str, str | None], None] | None = None,
        control_transport: object | None = None,
        status_callback: Callable[[AgentSnapshot], None] | None = None,
        stop_event: threading.Event | None = None,
    ) -> None:
        self.settings = settings
        self.device_id = device_id
        self.device_name = device_name
        self.status_callback = status_callback
        self.stop_event = stop_event or threading.Event()
        self.camera = camera or RTSPCamera(
            settings.rtsp_url,
            safe_url=settings.redacted_rtsp_url,
            open_timeout=settings.camera_open_timeout,
            read_timeout=settings.camera_read_timeout,
            reconnect_initial=settings.reconnect_initial,
            reconnect_max=settings.reconnect_max,
        )
        self.detector = detector or ComputerScreenDetector(
            settings.computer_model,
            confidence=settings.computer_confidence,
            image_size=settings.detector_image_size,
            normalized_roi=settings.monitor_roi,
        )
        self.classifier = classifier or FacebookClassifier(
            settings.classifier_model,
            torch_threads=settings.torch_threads,
        )
        self.decision = DecisionEngine(
            history_size=settings.history_size,
            minimum_history=settings.minimum_history,
            minimum_active_frames=settings.minimum_active_frames,
            active_frame_threshold=settings.active_frame_threshold,
            activate_average=settings.activate_average,
            deactivate_average=settings.deactivate_average,
        )
        self.rule_engine = MonitoringRuleEngine(
            rules_config or RulesConfig.from_toml(settings.rules_file)
        )
        self.physical_alarm = physical_alarm or NoopPhysicalAlarm()
        self.ptz = ptz or NoopPTZ()
        self.ptz_arbiter = PTZArbiter(self.ptz)
        self.auto_patrol = auto_patrol or AutoPatrol(AutoPatrolConfig())
        self.search_explorer = search_explorer or BoundedRandomSearch()
        self.feature_runtime = feature_runtime or FeatureRuntime(
            allowed={FeatureId.FACEBOOK_MONITOR},
            initial_feature=FeatureId.FACEBOOK_MONITOR,
        )
        self.person_detector = person_detector
        self.person_guard = person_guard or PersonGuard()
        self.command_store = command_store
        self.control_ack_callback = control_ack_callback
        self.control_transport = control_transport
        self.local_preview = local_preview or LocalPreviewStore.from_environment(
            device_id=device_id,
            max_width=settings.preview_max_width,
            max_height=settings.preview_max_height,
        )
        self.presence = presence or AgentPresence()
        self.publisher = MainfluxPublisher(
            enabled=settings.mainflux_enabled and not dry_run,
            url=settings.mainflux_messages_url,
            thing_key=settings.mainflux_thing_key,
            auth_scheme=settings.mainflux_auth_scheme,
            heartbeat_seconds=settings.mainflux_heartbeat,
            timeout=settings.mainflux_timeout,
            retry_backoff=settings.mainflux_retry_backoff,
            verify_tls=settings.mainflux_verify_tls,
            outbox_path=settings.mainflux_outbox_path,
            device_id=device_id,
            destination_id=settings.mainflux_destination_id,
        )
        self.preview = PreviewController(settings)
        self._last_displayed_state: AgentState | None = None
        self._offline_announced = False
        self._facebook_alarm_open = False
        self._person_auto_enabled = False
        self._person_violation_count = 0
        self._pending_control_applied = False
        self._last_runtime_state = self.feature_runtime.snapshot()

    def _process_control_commands(self) -> None:
        """Apply the small, typed command vocabulary owned by the PC server."""
        if self.command_store is None:
            return
        try:
            commands = self.command_store.claim(self.device_id)
        except Exception as exc:
            LOGGER.error("[%s] Control command store unavailable: %s", self.device_id, exc)
            return
        for command in commands:
            try:
                if command.action == "set_auto":
                    enabled = bool(command.payload["enabled"])
                    desired = self.feature_runtime.snapshot()
                    if not desired.runtime_enabled:
                        raise PTZError("auto requires a runtime-enabled device")
                    if desired.feature == FeatureId.NONE:
                        raise PTZError("auto requires an active feature")
                    if enabled and not self.ptz_arbiter.available:
                        raise PTZError("auto requires enabled PTZ")
                    if desired.feature == FeatureId.FACEBOOK_MONITOR:
                        self.auto_patrol.set_enabled(enabled)
                        self._person_auto_enabled = False
                        mode = "facebook patrol"
                    else:
                        self.auto_patrol.set_enabled(False)
                        self._person_auto_enabled = enabled
                        if enabled:
                            self.person_guard.reset_search_timer()
                        mode = "person tracking & search"
                    if not enabled:
                        self.ptz_arbiter.stop(force=True)
                        self.search_explorer.reset()
                    detail = (
                        f"auto={'on' if enabled else 'off'}"
                        if not enabled
                        else f"auto=on; {mode}"
                    )
                elif command.action == "move":
                    if self.auto_patrol.enabled or self._person_auto_enabled:
                        raise PTZError("manual move is rejected while auto is enabled")
                    direction = PTZMove(str(command.payload["direction"]))
                    duration = command.payload.get("duration_seconds")
                    self.ptz_arbiter.start(
                        "manual",
                        direction,
                        duration_seconds=None if duration is None else float(duration),
                    )
                    detail = f"moved {direction.value}"
                elif command.action == "stop":
                    self.ptz_arbiter.stop(force=True)
                    detail = "ptz stopped"
                elif command.action == "set_desired_state":
                    next_state = DesiredFeatureState(
                        runtime_enabled=bool(command.payload["runtime_enabled"]),
                        feature=FeatureId(str(command.payload["feature"])),
                        generation=int(command.payload["generation"]),
                    )
                    if next_state.feature == FeatureId.PERSON_GUARD and self.person_detector is None:
                        raise FeatureError("person_guard model is not installed and verified")
                    applied, changed = self.feature_runtime.apply(next_state)
                    if changed:
                        # A new desired state owns the camera immediately; no
                        # patrol/tracker segment may outlive its old feature.
                        self.ptz_arbiter.stop(force=True)
                        self.auto_patrol.set_enabled(False)
                        self._person_auto_enabled = False
                        self.person_guard.reset()
                        self.decision.reset()
                        self._facebook_alarm_open = False
                        self._pending_control_applied = True
                        self._last_runtime_state = applied
                    detail = (
                        f"feature={applied.feature.value} runtime={'on' if applied.runtime_enabled else 'standby'} "
                        f"generation={applied.generation}"
                    )
                    self._publish_control_ack(applied, "applied", None)
                else:  # CommandStore validation makes this fail-closed guard defensive.
                    raise ValueError("unsupported command")
            except (PTZError, FeatureError, ValueError, KeyError) as exc:
                self.command_store.complete(command.command_id, "rejected", str(exc))
                if command.action == "set_desired_state":
                    current = self.feature_runtime.snapshot()
                    self._publish_control_ack(current, "rejected", str(exc))
                LOGGER.warning("[%s] Control command rejected: %s", self.device_id, exc)
            except Exception as exc:
                self.command_store.complete(command.command_id, "failed", type(exc).__name__)
                LOGGER.error("[%s] Control command failed: %s", self.device_id, type(exc).__name__)
            else:
                self.command_store.complete(command.command_id, "executed", detail)
                LOGGER.info("[%s] Control command executed: %s", self.device_id, detail)

    def _publish_control_ack(
        self,
        state: DesiredFeatureState,
        status: str,
        detail: str | None,
    ) -> None:
        """Emit a safe local/MQTT acknowledgement without command contents."""

        if self.control_ack_callback is None:
            return
        try:
            self.control_ack_callback(self.device_id, state, status, detail)
        except Exception as exc:
            LOGGER.error("[%s] Control acknowledgement failed: %s", self.device_id, type(exc).__name__)

    def _control_worker(self, stop_event: threading.Event) -> None:
        """Poll the local command queue independently of inference.

        Inference can take noticeable time depending on workload; keeping queue
        polling in a dedicated thread keeps manual PTZ and auto switching responsive.
        The queue is durable, so a short polling interval is safe and commands are
        claimed atomically per device.
        """
        while not stop_event.is_set() and not self.stop_event.is_set():
            self._process_control_commands()
            stop_event.wait(0.03)

    def _apply_auto_patrol(
        self,
        *,
        screen_detected: bool,
        alarm_event: bool,
        detection: Detection | None = None,
    ) -> None:
        try:
            if self.auto_patrol.enabled and screen_detected and detection is not None:
                centering_move = screen_centering_move(detection)
                if centering_move is not None:
                    # Nudge PTZ to center and maximize the screen in frame
                    self.ptz_arbiter.start(
                        "facebook_patrol",
                        centering_move,
                        duration_seconds=0.25,
                    )
                    LOGGER.info(
                        "[%s] Auto patrol centering screen candidate: %s",
                        self.device_id,
                        centering_move.value,
                    )
                    return

            actions = self.auto_patrol.observe(
                screen_detected=screen_detected,
                alarm_event=alarm_event,
            )
            for action in actions:
                if action == PatrolAction.STOP:
                    self.ptz_arbiter.stop("facebook_patrol", force=False)
                elif action == PatrolAction.MOVE_NEXT:
                    # Non-blocking stochastic bounded search move.
                    direction = self.search_explorer.next_direction()
                    self.ptz_arbiter.start("facebook_patrol", direction)
                    LOGGER.info(
                        "[%s] Auto patrol moving to next view: %s",
                        self.device_id,
                        direction.value,
                    )
        except PTZError as exc:
            # Do not keep repeatedly moving after a camera/PTZ failure.
            self.auto_patrol.set_enabled(False)
            self.ptz_arbiter.stop(force=True)
            LOGGER.error("[%s] Auto patrol disabled after PTZ error: %s", self.device_id, exc)

    def _ensure_facebook_entry_pulse(
        self, decision: Decision, rule: RuleEvaluation
    ) -> RuleEvaluation:
        """Guarantee one publishable pulse when the decision enters Facebook.

        The RuleEngine remains the authority for temporal/cooldown evaluation.
        This is a fail-closed observability guard: a confirmed Facebook state
        must never silently reach Mainflux with no initial event pulse.
        """

        if decision.state != AgentState.FACEBOOK_DETECTED:
            self._facebook_alarm_open = False
            return rule
        if not self.rule_engine.config.facebook.enabled:
            return rule
        if self._facebook_alarm_open:
            return rule
        self._facebook_alarm_open = True
        if rule.event_triggered:
            return rule
        LOGGER.error(
            "Rule engine did not emit an entry pulse for confirmed Facebook; "
            "sending guarded Mainflux alarm pulse"
        )
        return replace(
            rule,
            status=RuleStatus.VIOLATION,
            event_triggered=True,
            violation_count=max(1, rule.violation_count + 1),
        )

    def _notify_status(
        self,
        *,
        extraction: ScreenExtraction,
        decision: Decision | None,
        rule: RuleEvaluation,
        raw_active_score: float,
        classifier_margin: float,
        camera_online: bool,
        person_evaluation: PersonEvaluation | None = None,
    ) -> None:
        if self.status_callback is None:
            return
        desired = self.feature_runtime.snapshot()
        self.status_callback(
            AgentSnapshot(
                device_id=self.device_id,
                device_name=self.device_name,
                extraction=extraction,
                decision=decision,
                rule=rule,
                raw_active_score=raw_active_score,
                classifier_margin=classifier_margin,
                camera_online=camera_online,
                updated_at=time.monotonic(),
                active_feature=desired.active_feature,
                runtime_enabled=desired.runtime_enabled,
                person_present=bool(person_evaluation and person_evaluation.present),
                person_tracking=bool(self._person_auto_enabled and person_evaluation and person_evaluation.present),
                control_generation=desired.generation,
            )
        )

    def _publish(self, telemetry: Telemetry, *, force: bool = False) -> None:
        try:
            self.publisher.publish(telemetry, force=force)
        except MainfluxError as exc:
            LOGGER.error("Mainflux publish failed: %s", exc)

    def _consume_control_applied_event(self) -> bool:
        pending = self._pending_control_applied
        self._pending_control_applied = False
        return pending

    def _publish_standby_status(self, state: DesiredFeatureState) -> None:
        """Publish a safe administrative state without a fake offline event."""

        telemetry = Telemetry(
            decision=None,
            camera_online=False,
            active_feature=FeatureId.NONE,
            runtime_enabled=False,
            control_generation=state.generation,
            control_applied_event=self._consume_control_applied_event(),
        )
        self._publish(telemetry, force=True)

    def _save_evidence(self, screen: np.ndarray) -> None:
        if not self.settings.save_evidence:
            return
        directory = self.settings.evidence_dir.resolve()
        directory.mkdir(parents=True, exist_ok=True)
        cutoff = datetime.now() - timedelta(days=self.settings.evidence_retention_days)
        for old_file in directory.glob("facebook_*.jpg"):
            try:
                if datetime.fromtimestamp(old_file.stat().st_mtime) < cutoff:
                    old_file.unlink()
            except OSError as exc:
                LOGGER.warning("Could not remove expired evidence %s: %s", old_file, exc)
        filename = directory / datetime.now().strftime("facebook_%Y%m%d_%H%M%S.jpg")
        if not cv2.imwrite(str(filename), screen):
            LOGGER.error("Could not save evidence to %s", filename)
        else:
            LOGGER.warning("Evidence saved: %s", filename)

    @staticmethod
    def _empty_extraction() -> ScreenExtraction:
        return ScreenExtraction(False, None, None)

    def _handle_offline(self) -> None:
        desired = self.feature_runtime.snapshot()
        if not desired.runtime_enabled:
            return
        if self.camera.seconds_since_frame < self.settings.camera_offline_after:
            return
        decision = self.decision.camera_offline()
        rule = self.rule_engine.update(decision)
        telemetry = Telemetry(
            decision=decision,
            camera_online=False,
            classifier_degraded=self.classifier.degraded,
            frame_age_seconds=self.camera.seconds_since_frame,
            rtsp_reconnect_count=self.camera.reconnect_count,
            self_preview_suppressed=self.preview.self_preview_suppressed,
            rule=rule,
            active_feature=desired.active_feature,
            runtime_enabled=True,
            control_generation=desired.generation,
            control_applied_event=self._consume_control_applied_event(),
        )
        self._publish(
            telemetry,
            force=(
                not self._offline_announced
                or rule.camera_offline_event
                or rule.camera_recovered_event
            ),
        )
        self.preview.show(
            frame=None,
            extraction=self._empty_extraction(),
            decision=decision,
            rule=rule,
            raw_active_score=0.0,
            classifier_margin=0.0,
            camera_online=False,
        )
        self._notify_status(
            extraction=self._empty_extraction(),
            decision=decision,
            rule=rule,
            raw_active_score=0.0,
            classifier_margin=0.0,
            camera_online=False,
        )
        if not self._offline_announced:
            LOGGER.error(
                "[%s] Camera is offline: %s",
                self.device_id,
                self.camera.last_error or "no frames",
            )
            self._offline_announced = True

    def _process_person_frame(
        self,
        *,
        frame: np.ndarray,
        captured_at: float,
        inference_started: float,
        desired: DesiredFeatureState,
    ) -> None:
        if self.person_detector is None:
            # This is normally rejected at desired-state application time.  It
            # remains a defensive guard for a malformed direct construction.
            raise FeatureError("person_guard model is not installed and verified")
        detection = self.person_detector.detect(frame)
        evaluation = self.person_guard.observe(detection)
        tracking = bool(self._person_auto_enabled and evaluation.present)
        try:
            if self._person_auto_enabled:
                if evaluation.present and detection is not None:
                    direction = self.person_guard.next_tracking_move(
                        detection, auto_enabled=True
                    )
                    if direction is not None:
                        self.ptz_arbiter.start(
                            "person_guard",
                            direction,
                            duration_seconds=self.person_guard.config.tracking_move_duration_seconds,
                        )
                    else:
                        self.ptz_arbiter.stop("person_guard", force=False)
                elif detection is None and not evaluation.present:
                    if self.person_guard.should_search_move(auto_enabled=True):
                        direction = self.search_explorer.next_direction()
                        self.ptz_arbiter.start(
                            "person_guard",
                            direction,
                            duration_seconds=self.person_guard.config.search_move_duration_seconds,
                        )
                        LOGGER.info(
                            "[%s] Person Guard search move: %s",
                            self.device_id,
                            direction.value,
                        )
                else:
                    self.ptz_arbiter.stop("person_guard", force=False)
        except PTZError as exc:
            self._person_auto_enabled = False
            self.ptz_arbiter.stop(force=True)
            LOGGER.error("[%s] Person Guard auto tracking disabled after PTZ error: %s", self.device_id, exc)

        if evaluation.alert_event:
            self._person_violation_count += 1
        rule = RuleEvaluation(
            status=RuleStatus.VIOLATION if evaluation.present else RuleStatus.NORMAL,
            event_cleared=evaluation.event_cleared,
            violation_count=self._person_violation_count,
        )
        telemetry = Telemetry(
            decision=None,
            camera_online=True,
            frame_age_seconds=max(0.0, time.monotonic() - captured_at),
            inference_ms=(time.perf_counter() - inference_started) * 1000,
            rtsp_reconnect_count=self.camera.reconnect_count,
            self_preview_suppressed=self.preview.self_preview_suppressed,
            rule=rule,
            active_feature=FeatureId.PERSON_GUARD,
            runtime_enabled=True,
            person_present=evaluation.present,
            person_confidence=evaluation.confidence,
            person_tracking=tracking,
            feature_alert_event=evaluation.alert_event,
            feature_event_cleared=evaluation.event_cleared,
            control_generation=desired.generation,
            control_applied_event=self._consume_control_applied_event(),
        )
        self._publish(
            telemetry,
            force=bool(
                evaluation.alert_event
                or evaluation.event_cleared
                or telemetry.control_applied_event
            ),
        )
        if evaluation.alert_event:
            LOGGER.warning("PERSON GUARD EVENT: anonymous person presence confirmed")
            self.physical_alarm.trigger("person_detected")
        self.preview.show_person_guard(
            frame=frame,
            evaluation=evaluation,
            camera_online=True,
            tracking=tracking,
        )
        self._notify_status(
            extraction=self._empty_extraction(),
            decision=None,
            rule=rule,
            raw_active_score=0.0,
            classifier_margin=0.0,
            camera_online=True,
            person_evaluation=evaluation,
        )

    def run(self, *, max_cycles: int | None = None) -> None:
        if self.classifier.degraded:
            LOGGER.warning(
                "Classifier has no 'other' class; results are degraded until the "
                "3-class model is trained"
            )
        if self.preview.mode == "full":
            LOGGER.warning(
                "Full camera preview can create recursive mirror on the monitored "
                "PC; press V for compact mode"
            )
        LOGGER.info(
            "[%s] Camera source: %s",
            self.device_id,
            self.camera.safe_url,
        )
        LOGGER.info(
            "Inference every %.2fs; detector=%spx; torch threads=%s; preview=%s",
            self.settings.process_interval,
            self.settings.detector_image_size,
            self.settings.torch_threads,
            self.preview.mode,
        )
        LOGGER.info("Classifier model version: %s", self.classifier.model_version)

        last_sequence = -1
        last_processed = 0.0
        cycles = 0
        camera_started = False
        standby_published_generation = -1
        last_control_presence = 0.0
        control_stop = threading.Event()
        control_thread: threading.Thread | None = None
        if self.command_store is not None:
            control_thread = threading.Thread(
                target=self._control_worker,
                args=(control_stop,),
                name=f"control-{self.device_id}",
                daemon=True,
            )
            control_thread.start()
            LOGGER.info("[%s] Control worker ready; polling local command queue", self.device_id)
        if self.control_transport is not None:
            try:
                start = getattr(self.control_transport, "start")
                start()
                self._publish_control_ack(self.feature_runtime.snapshot(), "online", None)
            except Exception as exc:
                LOGGER.error("[%s] Mainflux MQTT control unavailable: %s", self.device_id, type(exc).__name__)
        self.local_preview.clear(self.device_id)
        self.presence.clear(self.device_id)
        self.presence.touch(self.device_id)
        try:
            while not self.stop_event.is_set():
                self.presence.touch(self.device_id)
                desired = self.feature_runtime.snapshot()
                if (
                    self.control_transport is not None
                    and time.monotonic() - last_control_presence >= 15.0
                ):
                    self._publish_control_ack(desired, "online", None)
                    last_control_presence = time.monotonic()
                if not desired.runtime_enabled:
                    if camera_started:
                        self.ptz_arbiter.stop(force=True)
                        self.camera.stop()
                        camera_started = False
                        last_sequence = -1
                        last_processed = 0.0
                        self.local_preview.clear(self.device_id)
                    if standby_published_generation != desired.generation:
                        self._publish_standby_status(desired)
                        standby_published_generation = desired.generation
                    if self.preview.poll():
                        break
                    self.stop_event.wait(0.05)
                    continue
                if not camera_started:
                    self.camera.start()
                    camera_started = True
                    last_sequence = -1
                    last_processed = 0.0
                    self.decision.reset()
                    self.person_guard.reset()
                    self._offline_announced = False
                packet = self.camera.read_latest(after_sequence=last_sequence, timeout=0.05)
                if packet is None:
                    self._handle_offline()
                    if self.preview.poll():
                        break
                    continue
                last_sequence = packet.sequence
                self._offline_announced = False
                self.local_preview.publish(self.device_id, packet.frame)
                now = time.monotonic()
                if now - last_processed < self.settings.process_interval:
                    if self.preview.poll():
                        break
                    continue
                last_processed = now

                inference_started = time.perf_counter()
                frame = packet.frame
                if desired.feature == FeatureId.PERSON_GUARD:
                    self._process_person_frame(
                        frame=frame,
                        captured_at=packet.captured_at,
                        inference_started=inference_started,
                        desired=desired,
                    )
                    if self.preview.poll():
                        self.stop_event.set()
                        break
                    cycles += 1
                    if max_cycles is not None and cycles >= max_cycles:
                        break
                    continue
                if desired.feature == FeatureId.NONE:
                    telemetry = Telemetry(
                        decision=None,
                        camera_online=True,
                        frame_age_seconds=max(0.0, time.monotonic() - packet.captured_at),
                        rtsp_reconnect_count=self.camera.reconnect_count,
                        self_preview_suppressed=self.preview.self_preview_suppressed,
                        active_feature=FeatureId.NONE,
                        runtime_enabled=True,
                        control_generation=desired.generation,
                        control_applied_event=self._consume_control_applied_event(),
                    )
                    self._publish(telemetry, force=telemetry.control_applied_event)
                    self._notify_status(
                        extraction=self._empty_extraction(),
                        decision=None,
                        rule=telemetry.rule,
                        raw_active_score=0.0,
                        classifier_margin=0.0,
                        camera_online=True,
                    )
                    if self.preview.poll():
                        self.stop_event.set()
                        break
                    cycles += 1
                    if max_cycles is not None and cycles >= max_cycles:
                        break
                    continue
                extraction = self.detector.extract(frame)
                probabilities: dict[str, float] = {}
                raw_active_score = 0.0
                classifier_margin = 0.0
                if not extraction.computer_detected or extraction.screen is None:
                    result = self.decision.update(computer_detected=False)
                    computer_confidence = 0.0
                else:
                    probabilities = self.classifier.predict(extraction.screen)
                    raw_active_score = probabilities.get("facebook_active", 0.0)
                    runner_up = max(
                        (
                            score
                            for name, score in probabilities.items()
                            if name != "facebook_active"
                        ),
                        default=0.0,
                    )
                    classifier_margin = raw_active_score - runner_up
                    predicted_class = max(probabilities, key=probabilities.get)
                    evidence_score = (
                        raw_active_score
                        if predicted_class == "facebook_active"
                        and classifier_margin >= self.settings.active_min_margin
                        else 0.0
                    )
                    result = self.decision.update(
                        computer_detected=True,
                        active_score=evidence_score,
                    )
                    computer_confidence = (
                        extraction.detection.confidence if extraction.detection else 0.0
                    )

                inference_ms = (time.perf_counter() - inference_started) * 1000
                rule = self._ensure_facebook_entry_pulse(
                    result, self.rule_engine.update(result)
                )
                telemetry = Telemetry(
                    decision=result,
                    camera_online=True,
                    computer_confidence=computer_confidence,
                    classifier_degraded=self.classifier.degraded,
                    raw_active_score=raw_active_score,
                    classifier_margin=classifier_margin,
                    frame_age_seconds=max(0.0, time.monotonic() - packet.captured_at),
                    inference_ms=inference_ms,
                    rtsp_reconnect_count=self.camera.reconnect_count,
                    self_preview_suppressed=self.preview.self_preview_suppressed,
                    rule=rule,
                    active_feature=FeatureId.FACEBOOK_MONITOR,
                    runtime_enabled=True,
                    control_generation=desired.generation,
                    control_applied_event=self._consume_control_applied_event(),
                )
                self._publish(
                    telemetry,
                    force=bool(
                        rule.event_triggered
                        or rule.event_cleared
                        or rule.camera_offline_event
                        or rule.camera_recovered_event
                        or telemetry.control_applied_event
                    ),
                )

                if result.state != self._last_displayed_state:
                    LOGGER.info(
                        "State=%s active_avg=%.3f votes=%s/%s",
                        result.state.name,
                        result.active_score,
                        result.votes,
                        result.samples,
                    )
                    self._last_displayed_state = result.state
                if rule.event_triggered:
                    LOGGER.warning(
                        "RULE EVENT: Facebook usage active for %.1fs (event #%s)",
                        rule.active_seconds,
                        rule.violation_count,
                    )
                    self.physical_alarm.trigger("facebook_detected")
                    if extraction.screen is not None:
                        self._save_evidence(extraction.screen)

                self._apply_auto_patrol(
                    screen_detected=bool(
                        extraction.computer_detected and extraction.screen is not None
                    ),
                    alarm_event=rule.event_triggered,
                    detection=extraction.detection,
                )

                self.preview.show(
                    frame=frame,
                    extraction=extraction,
                    decision=result,
                    rule=rule,
                    raw_active_score=raw_active_score,
                    classifier_margin=classifier_margin,
                    camera_online=True,
                )
                self._notify_status(
                    extraction=extraction,
                    decision=result,
                    rule=rule,
                    raw_active_score=raw_active_score,
                    classifier_margin=classifier_margin,
                    camera_online=True,
                )
                if self.preview.poll():
                    self.stop_event.set()
                    break

                cycles += 1
                if max_cycles is not None and cycles >= max_cycles:
                    break
        finally:
            control_stop.set()
            if control_thread is not None:
                control_thread.join(timeout=1.0)
            if self.control_transport is not None:
                try:
                    stop = getattr(self.control_transport, "stop")
                    stop()
                except Exception:
                    pass
            self.local_preview.clear(self.device_id)
            self.presence.clear(self.device_id)
            try:
                self.ptz_arbiter.stop(force=True)
            except PTZError:
                pass
            if camera_started:
                self.camera.stop()
            if self.preview.enabled:
                cv2.destroyAllWindows()
            LOGGER.info("[%s] Agent stopped", self.device_id)
