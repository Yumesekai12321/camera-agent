from __future__ import annotations

import logging
import threading
import time

import cv2
import numpy as np

from .application import AgentSnapshot, CameraAgent
from .camera import build_device_camera
from .config import ConfigurationError
from .fleet_config import DeviceDefinition, FleetConfig
from .physical_alarm import build_tapo_alarm
from .vision import ComputerScreenDetector, FacebookClassifier


LOGGER = logging.getLogger(__name__)
FLEET_WINDOW = "Camera Agent - FLEET STATUS"
FLEET_WIDTH = 680
FLEET_HEADER_HEIGHT = 56
FLEET_ROW_HEIGHT = 82


def _short_ai_state(name: str) -> str:
    return {
        "NO_COMPUTER": "NO_PC",
        "COMPUTER_NO_FACEBOOK": "NO_FB",
        "FACEBOOK_DETECTED": "FACEBOOK",
        "CAMERA_OFFLINE": "OFFLINE",
    }.get(name, name[:12])


class FleetStatusBoard:
    """Thread-safe snapshot store rendered without camera pixels."""

    def __init__(self, devices: tuple[DeviceDefinition, ...]) -> None:
        self.devices = devices
        self._snapshots: dict[str, AgentSnapshot] = {}
        self._errors: dict[str, str] = {}
        self._lock = threading.Lock()

    def update(self, snapshot: AgentSnapshot) -> None:
        with self._lock:
            self._snapshots[snapshot.device_id] = snapshot
            self._errors.pop(snapshot.device_id, None)

    def fail(self, device_id: str, message: str) -> None:
        with self._lock:
            self._errors[device_id] = message

    def render(self, *, now: float | None = None) -> np.ndarray:
        current_time = time.monotonic() if now is None else now
        with self._lock:
            snapshots = dict(self._snapshots)
            errors = dict(self._errors)

        width = FLEET_WIDTH
        row_height = FLEET_ROW_HEIGHT
        height = FLEET_HEADER_HEIGHT + row_height * len(self.devices)
        panel = np.full((height, width, 3), (23, 23, 28), dtype=np.uint8)
        cv2.putText(
            panel,
            f"FLEET {len(self.devices)}   |   PIXELS HIDDEN   |   Q QUIT",
            (14, 34),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.62,
            (240, 240, 240),
            2,
        )

        for index, device in enumerate(self.devices):
            top = FLEET_HEADER_HEIGHT + index * row_height
            snapshot = snapshots.get(device.device_id)
            if device.device_id in errors:
                status = "ERROR"
                details = f"ERROR  {errors[device.device_id][:62]}"
                score = "Inspect the agent log"
                color = (30, 30, 235)
            elif snapshot is None:
                status = "STARTING"
                details = "CAM WAIT   SCREEN -   AI -   RULE -"
                score = "Waiting for first frame"
                color = (0, 180, 255)
            else:
                alert = snapshot.decision.facebook_active or snapshot.rule.event_triggered
                if alert:
                    status, color = "ALERT", (30, 30, 235)
                elif snapshot.camera_online:
                    status, color = "READY", (0, 195, 0)
                else:
                    status, color = "OFFLINE", (0, 180, 255)
                screen_found = bool(
                    snapshot.extraction.computer_detected
                    and snapshot.extraction.screen is not None
                )
                details = (
                    f"CAM {'ON' if snapshot.camera_online else 'OFF'}   "
                    f"SCREEN {'YES' if screen_found else 'NO'}   "
                    f"AI {_short_ai_state(snapshot.decision.state.name)}   "
                    f"RULE {snapshot.rule.status.name}"
                )
                age = max(0.0, current_time - snapshot.updated_at)
                score = (
                    f"raw {snapshot.raw_active_score:.3f}   "
                    f"avg {snapshot.decision.active_score:.3f}   age {age:.1f}s"
                )

            cv2.rectangle(
                panel,
                (6, top + 3),
                (width - 7, top + row_height - 4),
                color,
                2,
            )
            title = f"{device.name[:34]} [{device.device_id}]"
            cv2.putText(
                panel,
                title,
                (16, top + 23),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.48,
                color,
                1,
            )
            status_size = cv2.getTextSize(
                status,
                cv2.FONT_HERSHEY_SIMPLEX,
                0.52,
                2,
            )[0]
            cv2.putText(
                panel,
                status,
                (width - status_size[0] - 16, top + 23),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.52,
                color,
                2,
            )
            cv2.putText(
                panel,
                details,
                (16, top + 48),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.44,
                (230, 230, 230),
                1,
            )
            cv2.putText(
                panel,
                score,
                (16, top + 70),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.40,
                (165, 165, 175),
                1,
            )
        return panel


class FleetAgent:
    """Run independent camera state machines with one shared set of AI models."""

    def __init__(self, config: FleetConfig, *, dry_run: bool = False) -> None:
        if not config.devices:
            raise ConfigurationError(
                "Fleet runtime requires at least one enabled device; add or enable a device first"
            )
        self.config = config
        self.stop_event = threading.Event()
        self.board = FleetStatusBoard(config.devices)
        inference_lock = threading.Lock()
        self.classifier = FacebookClassifier(
            config.devices[0].settings.classifier_model,
            torch_threads=config.devices[0].settings.torch_threads,
            prediction_lock=inference_lock,
        )

        shared_detector_model = None
        if any(device.settings.monitor_roi is None for device in config.devices):
            from ultralytics import YOLO

            shared_detector_model = YOLO(
                str(config.devices[0].settings.computer_model)
            )

        self.agents: list[CameraAgent] = []
        for device in config.devices:
            camera = build_device_camera(device)
            detector = ComputerScreenDetector(
                device.settings.computer_model,
                confidence=device.settings.computer_confidence,
                image_size=device.settings.detector_image_size,
                normalized_roi=device.settings.monitor_roi,
                shared_model=shared_detector_model,
                prediction_lock=inference_lock,
            )
            self.agents.append(
                CameraAgent(
                    device.settings,
                    dry_run=dry_run,
                    device_id=device.device_id,
                    device_name=device.name,
                    detector=detector,
                    classifier=self.classifier,
                    camera=camera,
                    rules_config=device.rules_config,
                    physical_alarm=build_tapo_alarm(
                        enabled=device.physical_alarm_enabled,
                        host=device.settings.rtsp_host,
                        username=device.settings.rtsp_username,
                        password=device.settings.rtsp_password,
                        duration_seconds=device.physical_alarm_duration_seconds,
                        cooldown_seconds=device.physical_alarm_cooldown_seconds,
                    ),
                    status_callback=self.board.update,
                    stop_event=self.stop_event,
                )
            )

    def _run_agent(self, agent: CameraAgent, max_cycles: int | None) -> None:
        try:
            agent.run(max_cycles=max_cycles)
        except Exception as exc:  # Keep other cameras alive if one worker crashes.
            LOGGER.exception("[%s] Camera worker crashed", agent.device_id)
            self.board.fail(agent.device_id, str(exc) or type(exc).__name__)

    def run(self, *, max_cycles: int | None = None) -> None:
        LOGGER.info(
            "Fleet starting: %s camera(s), one shared classifier, serialized inference",
            len(self.agents),
        )
        threads = [
            threading.Thread(
                target=self._run_agent,
                args=(agent, max_cycles),
                name=f"agent-{agent.device_id}",
                daemon=True,
            )
            for agent in self.agents
        ]
        for thread in threads:
            thread.start()

        try:
            if self.config.preview:
                cv2.namedWindow(FLEET_WINDOW, cv2.WINDOW_AUTOSIZE)
            while any(thread.is_alive() for thread in threads):
                if self.config.preview:
                    cv2.imshow(FLEET_WINDOW, self.board.render())
                    if cv2.waitKey(50) & 0xFF == ord("q"):
                        self.stop_event.set()
                        break
                else:
                    self.stop_event.wait(0.25)
        finally:
            self.stop_event.set()
            for thread in threads:
                thread.join(timeout=12)
            if self.config.preview:
                cv2.destroyAllWindows()
            LOGGER.info("Fleet stopped")
