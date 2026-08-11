from __future__ import annotations

from dataclasses import dataclass
import ctypes
import logging
import os
import shutil
import subprocess
import threading
import time
from typing import Protocol, TYPE_CHECKING

import cv2
import numpy as np


LOGGER = logging.getLogger(__name__)


def _enable_per_monitor_dpi_awareness() -> bool:
    """Use physical window coordinates so MSS and Win32 agree at 125%/150% scaling."""
    if os.name != "nt":
        return False
    try:
        setter = ctypes.windll.user32.SetThreadDpiAwarenessContext
        setter.argtypes = [ctypes.c_void_p]
        setter.restype = ctypes.c_void_p
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
        return bool(setter(ctypes.c_void_p(-4)))
    except (AttributeError, OSError):
        LOGGER.debug("Per-monitor DPI awareness is unavailable", exc_info=True)
        return False

if TYPE_CHECKING:
    from .fleet_config import DeviceDefinition


@dataclass(frozen=True)
class FramePacket:
    frame: np.ndarray
    sequence: int
    captured_at: float


class FrameSource(Protocol):
    """Common latest-frame contract implemented by every capture adapter."""

    safe_url: str
    last_error: str | None
    reconnect_count: int

    @property
    def seconds_since_frame(self) -> float: ...

    def start(self) -> "FrameSource": ...

    def stop(self) -> None: ...

    def read_latest(
        self,
        *,
        after_sequence: int = -1,
        timeout: float = 1.0,
    ) -> FramePacket | None: ...


class RTSPCamera:
    """Continuously drain RTSP and expose only the newest decoded frame."""

    def __init__(
        self,
        url: str,
        *,
        safe_url: str,
        open_timeout: float = 8.0,
        read_timeout: float = 8.0,
        reconnect_initial: float = 1.0,
        reconnect_max: float = 15.0,
    ) -> None:
        self._url = url
        self.safe_url = safe_url
        self.open_timeout = open_timeout
        self.read_timeout = read_timeout
        self.reconnect_initial = reconnect_initial
        self.reconnect_max = reconnect_max
        self._condition = threading.Condition()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._packet: FramePacket | None = None
        self._sequence = 0
        self._started_at = time.monotonic()
        self.last_frame_at = 0.0
        self.last_error: str | None = None
        self.connected = False
        self.reconnect_count = 0

    def start(self) -> "RTSPCamera":
        if self._thread and self._thread.is_alive():
            return self
        self._stop.clear()
        self._started_at = time.monotonic()
        self._thread = threading.Thread(
            target=self._capture_loop,
            name=f"video-reader-{self.safe_url.split(':', 1)[0]}",
            daemon=True,
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        with self._condition:
            self._condition.notify_all()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=self.read_timeout + 2)

    @property
    def seconds_since_frame(self) -> float:
        reference = self.last_frame_at or self._started_at
        return max(0.0, time.monotonic() - reference)

    def read_latest(
        self,
        *,
        after_sequence: int = -1,
        timeout: float = 1.0,
    ) -> FramePacket | None:
        deadline = time.monotonic() + timeout
        with self._condition:
            while not self._stop.is_set():
                if self._packet is not None and self._packet.sequence > after_sequence:
                    return self._packet
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._condition.wait(remaining)
        return None

    def _new_capture(self) -> cv2.VideoCapture:
        os.environ.setdefault(
            "OPENCV_FFMPEG_CAPTURE_OPTIONS",
            "rtsp_transport;tcp|max_delay;500000",
        )
        capture = cv2.VideoCapture()
        parameters: list[int] = []
        if hasattr(cv2, "CAP_PROP_OPEN_TIMEOUT_MSEC"):
            parameters.extend(
                [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, int(self.open_timeout * 1000)]
            )
        if hasattr(cv2, "CAP_PROP_READ_TIMEOUT_MSEC"):
            parameters.extend(
                [cv2.CAP_PROP_READ_TIMEOUT_MSEC, int(self.read_timeout * 1000)]
            )
        capture.open(self._url, cv2.CAP_FFMPEG, parameters)
        capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        return capture

    def _capture_loop(self) -> None:
        backoff = self.reconnect_initial
        while not self._stop.is_set():
            capture: cv2.VideoCapture | None = None
            try:
                LOGGER.info("Connecting to camera: %s", self.safe_url)
                capture = self._new_capture()
                if not capture.isOpened():
                    raise RuntimeError("Video source could not be opened")
                self.connected = True
                self.last_error = None
                backoff = self.reconnect_initial
                LOGGER.info("Camera connected")
                while not self._stop.is_set():
                    ok, frame = capture.read()
                    if not ok or frame is None or frame.size == 0:
                        raise RuntimeError("Video frame read failed")
                    now = time.monotonic()
                    with self._condition:
                        self._sequence += 1
                        self.last_frame_at = now
                        self._packet = FramePacket(frame, self._sequence, now)
                        self._condition.notify_all()
            except Exception as exc:  # OpenCV raises backend-dependent errors.
                self.connected = False
                self.last_error = str(exc)
                if not self._stop.is_set():
                    self.reconnect_count += 1
                    LOGGER.warning("Camera unavailable (%s); retrying in %.1fs", exc, backoff)
                    self._stop.wait(backoff)
                    backoff = min(self.reconnect_max, backoff * 2)
            finally:
                if capture is not None:
                    capture.release()
        self.connected = False

    def __enter__(self) -> "RTSPCamera":
        return self.start()

    def __exit__(self, *_: object) -> None:
        self.stop()


class WebcamCamera(RTSPCamera):
    """Read a physical or virtual Windows webcam with latest-frame semantics."""

    BACKENDS = {
        "auto": cv2.CAP_ANY,
        "dshow": cv2.CAP_DSHOW,
        "msmf": cv2.CAP_MSMF,
    }

    def __init__(
        self,
        device_index: int,
        *,
        backend: str = "dshow",
        width: int | None = None,
        height: int | None = None,
        read_timeout: float = 8.0,
        reconnect_initial: float = 1.0,
        reconnect_max: float = 15.0,
    ) -> None:
        normalized_backend = backend.strip().lower()
        if normalized_backend not in self.BACKENDS:
            raise ValueError(
                f"Unsupported webcam backend {backend!r}; use auto, dshow or msmf"
            )
        super().__init__(
            str(device_index),
            safe_url=f"webcam:{device_index} ({normalized_backend})",
            open_timeout=read_timeout,
            read_timeout=read_timeout,
            reconnect_initial=reconnect_initial,
            reconnect_max=reconnect_max,
        )
        self.device_index = device_index
        self.backend = normalized_backend
        self.width = width
        self.height = height

    def _new_capture(self) -> cv2.VideoCapture:
        capture = cv2.VideoCapture(
            self.device_index,
            self.BACKENDS[self.backend],
        )
        if self.width is not None:
            capture.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        if self.height is not None:
            capture.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        return capture


class ADBCamera(RTSPCamera):
    """Capture the foreground Android display without depending on a visible window."""

    def __init__(
        self,
        device_serial: str | None = None,
        *,
        frames_per_second: float = 1.0,
        command_timeout: float = 6.0,
        require_landscape: bool = False,
        minimum_frame_std: float = 2.0,
        reconnect_initial: float = 1.0,
        reconnect_max: float = 15.0,
    ) -> None:
        if frames_per_second <= 0:
            raise ValueError("ADB capture FPS must be greater than zero")
        if command_timeout <= 0:
            raise ValueError("ADB command timeout must be greater than zero")
        if minimum_frame_std < 0:
            raise ValueError("ADB minimum frame standard deviation cannot be negative")
        serial = (device_serial or "").strip() or None
        super().__init__(
            serial or "android-display",
            safe_url="adb:android-display",
            open_timeout=command_timeout,
            read_timeout=command_timeout,
            reconnect_initial=reconnect_initial,
            reconnect_max=reconnect_max,
        )
        self.device_serial = serial
        self.frames_per_second = frames_per_second
        self.command_timeout = command_timeout
        self.require_landscape = require_landscape
        self.minimum_frame_std = minimum_frame_std

    def _capture_frame(self) -> np.ndarray:
        adb = shutil.which("adb")
        if adb is None:
            raise RuntimeError("adb is not available in PATH")
        command = [adb]
        if self.device_serial:
            command.extend(["-s", self.device_serial])
        command.extend(["exec-out", "screencap", "-p"])
        try:
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                timeout=self.command_timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("Android screenshot timed out") from exc
        except OSError as exc:
            raise RuntimeError(f"Could not execute adb: {exc}") from exc
        if result.returncode != 0:
            message = result.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(message[:240] or "adb screenshot failed")
        encoded = np.frombuffer(result.stdout, dtype=np.uint8)
        frame = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
        if frame is None or frame.size == 0:
            raise RuntimeError("Android screenshot was not a valid PNG frame")
        height, width = frame.shape[:2]
        if self.require_landscape and width <= height:
            raise RuntimeError(
                "Android display is not landscape; keep the configured live view in landscape"
            )
        if self.minimum_frame_std > 0 and float(frame.std()) < self.minimum_frame_std:
            raise RuntimeError(
                "Android display is almost uniform or black; wake the device and reopen "
                "the configured live view"
            )
        return frame

    def _capture_loop(self) -> None:
        backoff = self.reconnect_initial
        interval = 1.0 / self.frames_per_second
        while not self._stop.is_set():
            try:
                frame = self._capture_frame()
                now = time.monotonic()
                self.connected = True
                self.last_error = None
                backoff = self.reconnect_initial
                with self._condition:
                    self._sequence += 1
                    self.last_frame_at = now
                    self._packet = FramePacket(frame, self._sequence, now)
                    self._condition.notify_all()
                self._stop.wait(interval)
            except Exception as exc:
                self.connected = False
                self.last_error = str(exc)
                if not self._stop.is_set():
                    self.reconnect_count += 1
                    LOGGER.warning(
                        "Android display unavailable (%s); retrying in %.1fs",
                        exc,
                        backoff,
                    )
                    self._stop.wait(backoff)
                    backoff = min(self.reconnect_max, backoff * 2)
        self.connected = False


class WindowCamera(RTSPCamera):
    """Capture a named Windows window without requiring a virtual webcam."""

    METHODS = {"auto", "printwindow", "screen"}

    def __init__(
        self,
        window_title: str,
        *,
        capture_method: str = "auto",
        frames_per_second: float = 8.0,
        minimum_frame_std: float = 2.0,
        read_timeout: float = 8.0,
        reconnect_initial: float = 1.0,
        reconnect_max: float = 15.0,
    ) -> None:
        title = window_title.strip()
        if not title:
            raise ValueError("Window title cannot be empty")
        method = capture_method.strip().lower()
        if method not in self.METHODS:
            raise ValueError(
                "Unsupported window capture method; use auto, printwindow or screen"
            )
        if frames_per_second <= 0:
            raise ValueError("Window capture FPS must be greater than zero")
        if minimum_frame_std < 0:
            raise ValueError("Window minimum frame standard deviation cannot be negative")
        super().__init__(
            title,
            safe_url=f"window:{title}",
            open_timeout=read_timeout,
            read_timeout=read_timeout,
            reconnect_initial=reconnect_initial,
            reconnect_max=reconnect_max,
        )
        self.window_title = title
        self.capture_method = method
        self.frames_per_second = frames_per_second
        self.minimum_frame_std = minimum_frame_std
        self._active_capture_method: str | None = None

    @property
    def active_capture_method(self) -> str:
        """Return the backend currently producing frames without exposing camera data."""
        return self._active_capture_method or self.capture_method

    def _find_window(self) -> int:
        import win32gui

        handle = int(win32gui.FindWindow(None, self.window_title))
        if not handle:
            raise RuntimeError(f"Window not found: {self.window_title}")
        return handle

    @staticmethod
    def _grab_printwindow(handle: int) -> np.ndarray:
        import win32gui
        import win32ui

        left, top, right, bottom = win32gui.GetClientRect(handle)
        width = right - left
        height = bottom - top
        if width < 2 or height < 2:
            raise RuntimeError("Window client area is empty")

        window_dc = win32gui.GetDC(handle)
        source_dc = win32ui.CreateDCFromHandle(window_dc)
        memory_dc = source_dc.CreateCompatibleDC()
        bitmap = win32ui.CreateBitmap()
        try:
            bitmap.CreateCompatibleBitmap(source_dc, width, height)
            memory_dc.SelectObject(bitmap)
            result = ctypes.windll.user32.PrintWindow(
                handle,
                memory_dc.GetSafeHdc(),
                3,  # PW_CLIENTONLY | PW_RENDERFULLCONTENT
            )
            if result != 1:
                raise RuntimeError("PrintWindow did not return a frame")
            frame = np.frombuffer(bitmap.GetBitmapBits(True), dtype=np.uint8)
            frame = frame.reshape((height, width, 4))
            return cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)
        finally:
            win32gui.DeleteObject(bitmap.GetHandle())
            memory_dc.DeleteDC()
            source_dc.DeleteDC()
            win32gui.ReleaseDC(handle, window_dc)

    @staticmethod
    def _grab_screen(handle: int) -> np.ndarray:
        import mss
        import win32gui

        _enable_per_monitor_dpi_awareness()
        if win32gui.IsIconic(handle):
            raise RuntimeError("Window is minimized; restore it for screen capture")
        left, top = win32gui.ClientToScreen(handle, (0, 0))
        client = win32gui.GetClientRect(handle)
        width = client[2] - client[0]
        height = client[3] - client[1]
        if width < 2 or height < 2:
            raise RuntimeError("Window client area is empty")
        with mss.mss() as capture:
            image = np.asarray(
                capture.grab(
                    {
                        "left": left,
                        "top": top,
                        "width": width,
                        "height": height,
                    }
                )
            )
        return np.ascontiguousarray(image[:, :, :3])

    def _validate_window_frame(self, frame: np.ndarray) -> np.ndarray:
        if frame is None or frame.size == 0:
            raise RuntimeError("Window capture returned an empty frame")
        if self.minimum_frame_std > 0 and float(frame.std()) < self.minimum_frame_std:
            raise RuntimeError(
                "Window frame is almost uniform or black; the video surface was not captured"
            )
        return frame

    def _grab_with_method(self, handle: int, method: str) -> np.ndarray:
        grabber = self._grab_printwindow if method == "printwindow" else self._grab_screen
        return self._validate_window_frame(grabber(handle))

    def _grab_window(self, handle: int) -> np.ndarray:
        if self.capture_method != "auto":
            self._active_capture_method = self.capture_method
            return self._grab_with_method(handle, self.capture_method)

        preferred = self._active_capture_method or "printwindow"
        methods = (preferred, "screen" if preferred == "printwindow" else "printwindow")
        failures: list[str] = []
        for method in methods:
            try:
                frame = self._grab_with_method(handle, method)
            except Exception as exc:
                failures.append(f"{method}: {exc}")
                continue
            if method != self._active_capture_method:
                if self._active_capture_method is None and method == "printwindow":
                    LOGGER.info("Window capture selected PrintWindow")
                else:
                    LOGGER.warning("Window capture switched to %s", method)
            self._active_capture_method = method
            return frame
        raise RuntimeError("Window capture failed (" + "; ".join(failures) + ")")

    def _capture_loop(self) -> None:
        _enable_per_monitor_dpi_awareness()
        backoff = self.reconnect_initial
        interval = 1.0 / self.frames_per_second
        while not self._stop.is_set():
            try:
                handle = self._find_window()
                self._active_capture_method = None
                LOGGER.info("Capturing camera source: %s", self.safe_url)
                self.connected = True
                self.last_error = None
                backoff = self.reconnect_initial
                while not self._stop.is_set():
                    if handle != self._find_window():
                        raise RuntimeError("Window handle changed")
                    frame = self._grab_window(handle)
                    now = time.monotonic()
                    with self._condition:
                        self._sequence += 1
                        self.last_frame_at = now
                        self._packet = FramePacket(frame, self._sequence, now)
                        self._condition.notify_all()
                    self._stop.wait(interval)
            except Exception as exc:
                self.connected = False
                self.last_error = str(exc)
                if not self._stop.is_set():
                    self.reconnect_count += 1
                    LOGGER.warning(
                        "Window source unavailable (%s); retrying in %.1fs",
                        exc,
                        backoff,
                    )
                    self._stop.wait(backoff)
                    backoff = min(self.reconnect_max, backoff * 2)
        self.connected = False


def build_device_camera(device: DeviceDefinition) -> RTSPCamera:
    """Create the configured reader without exposing source credentials in logs."""
    settings = device.settings
    if device.source_type == "webcam":
        if device.webcam_index is None:
            raise ValueError(f"Webcam index is missing for {device.device_id}")
        return WebcamCamera(
            device.webcam_index,
            backend=device.webcam_backend,
            width=device.webcam_width,
            height=device.webcam_height,
            read_timeout=settings.camera_read_timeout,
            reconnect_initial=settings.reconnect_initial,
            reconnect_max=settings.reconnect_max,
        )
    if device.source_type == "window":
        if not device.window_title:
            raise ValueError(f"Window title is missing for {device.device_id}")
        return WindowCamera(
            device.window_title,
            capture_method=device.window_capture_method,
            frames_per_second=device.window_fps,
            minimum_frame_std=device.window_minimum_frame_std,
            read_timeout=settings.camera_read_timeout,
            reconnect_initial=settings.reconnect_initial,
            reconnect_max=settings.reconnect_max,
        )
    if device.source_type == "adb":
        return ADBCamera(
            device.adb_serial,
            frames_per_second=device.adb_fps,
            command_timeout=device.adb_command_timeout,
            require_landscape=device.adb_require_landscape,
            minimum_frame_std=device.adb_minimum_frame_std,
            reconnect_initial=settings.reconnect_initial,
            reconnect_max=settings.reconnect_max,
        )
    return RTSPCamera(
        settings.rtsp_url,
        safe_url=settings.redacted_rtsp_url,
        open_timeout=settings.camera_open_timeout,
        read_timeout=settings.camera_read_timeout,
        reconnect_initial=settings.reconnect_initial,
        reconnect_max=settings.reconnect_max,
    )
