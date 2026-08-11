from __future__ import annotations

from datetime import datetime, timedelta
from dataclasses import dataclass, replace
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
from .rules import MonitoringRuleEngine, RuleEvaluation, RulesConfig, RuleStatus
from .vision import ComputerScreenDetector, FacebookClassifier, ScreenExtraction


LOGGER = logging.getLogger(__name__)
FULL_WINDOW = "Camera Agent - FULL CAMERA FRAME"
COMPACT_WINDOW = "Camera Agent - COMPACT STATUS"
CLASSIFIER_WINDOW = "Camera Agent - Classifier input"


@dataclass(frozen=True)
class AgentSnapshot:
    device_id: str
    device_name: str
    extraction: ScreenExtraction
    decision: Decision
    rule: RuleEvaluation
    raw_active_score: float
    classifier_margin: float
    camera_online: bool
    updated_at: float


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
        decision: Decision,
        rule: RuleEvaluation,
        raw_active_score: float,
        classifier_margin: float,
        camera_online: bool,
    ) -> None:
        if self.status_callback is None:
            return
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
            )
        )

    def _publish(self, telemetry: Telemetry, *, force: bool = False) -> None:
        try:
            self.publisher.publish(telemetry, force=force)
        except MainfluxError as exc:
            LOGGER.error("Mainflux publish failed: %s", exc)

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
        self.camera.start()
        try:
            while not self.stop_event.is_set():
                packet = self.camera.read_latest(after_sequence=last_sequence, timeout=0.25)
                if packet is None:
                    self._handle_offline()
                    if self.preview.poll():
                        break
                    continue
                last_sequence = packet.sequence
                self._offline_announced = False
                now = time.monotonic()
                if now - last_processed < self.settings.process_interval:
                    if self.preview.poll():
                        break
                    continue
                last_processed = now

                inference_started = time.perf_counter()
                frame = packet.frame
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
                )
                self._publish(
                    telemetry,
                    force=bool(
                        rule.event_triggered
                        or rule.event_cleared
                        or rule.camera_offline_event
                        or rule.camera_recovered_event
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
            self.camera.stop()
            if self.preview.enabled:
                cv2.destroyAllWindows()
            LOGGER.info("[%s] Agent stopped", self.device_id)
