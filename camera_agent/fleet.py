from __future__ import annotations

import logging
import threading
import time

import cv2
import numpy as np

from .application import AgentSnapshot, CameraAgent
from .auto_patrol import AutoPatrol, AutoPatrolConfig
from .camera import build_device_camera
from .config import ConfigurationError
from .control import CommandStore
from .features import FeatureRuntime, FeatureId
from .fleet_config import DeviceDefinition, FleetConfig
from .physical_alarm import build_tapo_alarm
from .person_guard import PersonGuard
from .mainflux_control import MainfluxAgentControl, MQTTControlSettings
from .ptz import NoopPTZ, OnvifPTZ, PTZController
from .pentest import PentestStatus
from .pentest_agent import PentestAgent, PentestSnapshot
from .vision import ComputerScreenDetector, FacebookClassifier, PersonDetector
from .cyber.integration import CyberRuntimeBridge


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

    def update(self, snapshot: AgentSnapshot | PentestSnapshot) -> None:
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
            elif device.agent_type == "pentest":
                if snapshot is None:
                    status = "STARTING"
                    details = "SCAN -   FINDINGS -   PORTS -"
                    score = "Waiting for first bounded scan"
                    color = (0, 180, 255)
                else:
                    pentest = snapshot
                    assert isinstance(pentest, PentestSnapshot)
                    status = {
                        PentestStatus.PASS: "PASS",
                        PentestStatus.FINDINGS: "FINDINGS",
                        PentestStatus.ERROR: "ERROR",
                    }.get(pentest.status, "STARTING")
                    color = (
                        (30, 30, 235)
                        if pentest.status == PentestStatus.FINDINGS
                        else (0, 195, 0)
                        if pentest.status == PentestStatus.PASS
                        else (0, 180, 255)
                    )
                    details = (
                        f"CAM {'ON' if pentest.camera_online else 'OFF'}   "
                        f"SCAN {'OK' if pentest.scan_ok else 'ERROR'}   "
                        f"FINDINGS {pentest.finding_count}   "
                        f"HIGH {pentest.high_count}   PORTS {pentest.open_port_count}"
                    )
                    age = max(0.0, current_time - pentest.updated_at)
                    score = (
                        f"medium {pentest.medium_count}   low {pentest.low_count}   age {age:.1f}s"
                    )
            else:
                person_mode = snapshot.active_feature == FeatureId.PERSON_GUARD
                alert = (
                    snapshot.person_present
                    if person_mode
                    else bool(snapshot.decision and snapshot.decision.facebook_active)
                ) or snapshot.rule.event_triggered
                if alert:
                    status, color = "ALERT", (30, 30, 235)
                elif not snapshot.runtime_enabled:
                    status, color = "STANDBY", (120, 120, 160)
                elif snapshot.camera_online:
                    status, color = "READY", (0, 195, 0)
                else:
                    status, color = "OFFLINE", (0, 180, 255)
                screen_found = bool(
                    snapshot.extraction.computer_detected
                    and snapshot.extraction.screen is not None
                )
                if person_mode:
                    details = (
                        f"CAM {'ON' if snapshot.camera_online else 'OFF'}   "
                        f"PERSON {'YES' if snapshot.person_present else 'NO'}   "
                        f"TRACK {'ON' if snapshot.person_tracking else 'OFF'}   "
                        f"RULE {snapshot.rule.status.name}"
                    )
                elif snapshot.decision is None:
                    details = (
                        f"CAM {'ON' if snapshot.camera_online else 'OFF'}   "
                        f"FEATURE NONE   RULE {snapshot.rule.status.name}"
                    )
                else:
                    details = (
                        f"CAM {'ON' if snapshot.camera_online else 'OFF'}   "
                        f"SCREEN {'YES' if screen_found else 'NO'}   "
                        f"AI {_short_ai_state(snapshot.decision.state.name)}   "
                        f"RULE {snapshot.rule.status.name}"
                    )
                age = max(0.0, current_time - snapshot.updated_at)
                score = (
                    f"person {'present' if snapshot.person_present else 'clear'}   age {age:.1f}s"
                    if person_mode
                    else f"raw {snapshot.raw_active_score:.3f}   "
                    f"avg {snapshot.decision.active_score if snapshot.decision else 0.0:.3f}   age {age:.1f}s"
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
        self.cyber_bridge = CyberRuntimeBridge.from_environment()
        inference_lock = threading.Lock()
        camera_devices = tuple(device for device in config.devices if device.agent_type == "camera")
        self.classifier = None
        if camera_devices:
            self.classifier = FacebookClassifier(
                camera_devices[0].settings.classifier_model,
                torch_threads=camera_devices[0].settings.torch_threads,
                prediction_lock=inference_lock,
            )

        shared_detector_model = None
        if any(device.settings.monitor_roi is None for device in camera_devices):
            from ultralytics import YOLO

            shared_detector_model = YOLO(
                str(camera_devices[0].settings.computer_model)
            )

        shared_person_models: dict[tuple[str, str], object] = {}

        def build_person_detector(device: DeviceDefinition) -> PersonDetector | None:
            if FeatureId.PERSON_GUARD not in device.feature_allowed:
                return None
            if device.person_model_path is None or not device.person_model_sha256:
                LOGGER.warning(
                    "[%s] Person Guard is installed but model references are not configured; selection will be rejected",
                    device.device_id,
                )
                return None
            try:
                PersonDetector.verify_model(device.person_model_path, device.person_model_sha256)
                key = (str(device.person_model_path.resolve()), device.person_model_sha256)
                shared = shared_person_models.get(key)
                if shared is None:
                    from ultralytics import YOLO

                    shared = YOLO(str(device.person_model_path))
                    shared_person_models[key] = shared
                return PersonDetector(
                    device.person_model_path,
                    expected_sha256=device.person_model_sha256,
                    confidence=device.person_guard_config.minimum_confidence,
                    image_size=device.settings.detector_image_size,
                    shared_model=shared,
                    prediction_lock=inference_lock,
                )
            except (FileNotFoundError, ValueError) as exc:
                LOGGER.warning("[%s] Person Guard unavailable: %s", device.device_id, exc)
                return None

        self.agents: list[CameraAgent | PentestAgent] = []
        command_store = (
            CommandStore(camera_devices[0].settings.mainflux_outbox_path.parent / "control-commands.sqlite3")
            if camera_devices
            else None
        )
        control_settings = MQTTControlSettings.from_environment()
        for device in config.devices:
            if device.agent_type == "pentest":
                self.agents.append(
                    PentestAgent(
                        device,
                        dry_run=dry_run,
                        status_callback=self.board.update,
                        stop_event=self.stop_event,
                    )
                )
                continue
            camera = build_device_camera(device)
            ptz: PTZController = NoopPTZ()
            if device.ptz_enabled:
                ptz = OnvifPTZ(
                    host=device.settings.rtsp_host or "",
                    port=device.ptz_port,
                    username=device.settings.rtsp_username or "",
                    password=device.settings.rtsp_password or "",
                    profile_token=device.ptz_profile_token,
                    velocity=device.ptz_velocity,
                    move_duration_seconds=device.ptz_move_duration_seconds,
                )
            detector = ComputerScreenDetector(
                device.settings.computer_model,
                confidence=device.settings.computer_confidence,
                image_size=device.settings.detector_image_size,
                normalized_roi=device.settings.monitor_roi,
                shared_model=shared_detector_model,
                prediction_lock=inference_lock,
            )
            person_detector = build_person_detector(device)
            control_transport = (
                MainfluxAgentControl(
                    control_settings,
                    device_id=device.device_id,
                    thing_id=device.mainflux_thing_id,
                    thing_key=device.settings.mainflux_thing_key,
                    control_channel_id=device.mainflux_control_channel_id,
                    command_store=command_store,
                )
                if command_store is not None
                else None
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
                        audio_id=device.physical_alarm_audio_id,
                    ),
                    ptz=ptz,
                    auto_patrol=AutoPatrol(
                        AutoPatrolConfig(
                            enabled=device.auto_patrol_enabled,
                            observe_seconds=device.auto_patrol_observe_seconds,
                            search_move_interval_seconds=device.auto_patrol_search_move_interval_seconds,
                            max_alarm_events_per_screen=device.auto_patrol_max_alarm_events_per_screen,
                        )
                    ),
                    feature_runtime=FeatureRuntime(
                        allowed=device.feature_allowed,
                        initial_feature=device.feature_default,
                    ),
                    person_detector=person_detector,
                    person_guard=PersonGuard(device.person_guard_config),
                    command_store=command_store,
                    control_ack_callback=(control_transport.publish_ack if control_transport is not None else None),
                    control_transport=control_transport,
                    status_callback=self._camera_status,
                    stop_event=self.stop_event,
                )
            )

    def _run_agent(self, agent: CameraAgent, max_cycles: int | None) -> None:
        try:
            agent.run(max_cycles=max_cycles)
        except Exception as exc:  # Keep other cameras alive if one worker crashes.
            LOGGER.exception("[%s] Camera worker crashed", agent.device_id)
            self.board.fail(agent.device_id, str(exc) or type(exc).__name__)

    def _camera_status(self, snapshot: AgentSnapshot) -> None:
        self.board.update(snapshot)
        if self.cyber_bridge is not None:
            self.cyber_bridge.submit(snapshot)

    def run(self, *, max_cycles: int | None = None) -> None:
        LOGGER.info(
            "Fleet starting: %s device(s), camera models shared when needed, serialized inference",
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
            if self.cyber_bridge is not None:
                self.cyber_bridge.close()
            LOGGER.info("Fleet stopped")
