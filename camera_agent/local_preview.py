"""Latest-frame local preview shared by the agent and the loopback UI.

This is intentionally a one-frame store, not a recording buffer.  It is
opt-in, stays under ``runtime/previews`` by default and is never sent to
Mainflux.  The control server serves it only after the local control token has
been presented.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import threading
import time

import cv2
import numpy as np

from .config import PROJECT_DIR


_DEVICE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


class LocalPreviewStore:
    """Atomically replace one JPEG per device and never retain a frame queue."""

    def __init__(
        self,
        root: Path | None = None,
        *,
        enabled: bool = False,
        max_width: int = 1280,
        max_height: int = 720,
        jpeg_quality: int = 82,
        minimum_interval_seconds: float = 0.2,
    ) -> None:
        if max_width < 320 or max_height < 180:
            raise ValueError("preview dimensions are too small")
        if not 40 <= jpeg_quality <= 95:
            raise ValueError("jpeg_quality must be between 40 and 95")
        if minimum_interval_seconds < 0:
            raise ValueError("minimum_interval_seconds must not be negative")
        self.root = (root or PROJECT_DIR / "runtime" / "previews").resolve()
        self.enabled = bool(enabled)
        self.max_width = max_width
        self.max_height = max_height
        self.jpeg_quality = jpeg_quality
        self.minimum_interval_seconds = minimum_interval_seconds
        self._last_publish = 0.0
        self._lock = threading.Lock()

    @classmethod
    def from_environment(cls, *, device_id: str, max_width: int, max_height: int) -> "LocalPreviewStore":
        del device_id  # validation occurs in path_for; this keeps the factory side-effect free.
        return cls(
            enabled=_truthy(os.environ.get("LOCAL_PREVIEW_ENABLED")),
            max_width=max_width,
            max_height=max_height,
        )

    def path_for(self, device_id: str) -> Path:
        if not isinstance(device_id, str) or not _DEVICE_ID.fullmatch(device_id):
            raise ValueError("invalid device id for preview")
        return self.root / f"{device_id}.jpg"

    def clear(self, device_id: str) -> None:
        path = self.path_for(device_id)
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass
        for temporary in path.parent.glob(f".{path.name}.*.tmp"):
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass

    def publish(self, device_id: str, frame: np.ndarray, *, now: float | None = None) -> bool:
        if not self.enabled or frame is None or getattr(frame, "size", 0) == 0:
            return False
        current = time.monotonic() if now is None else now
        with self._lock:
            if current - self._last_publish < self.minimum_interval_seconds:
                return False
            self._last_publish = current
        image = self._fit(frame)
        ok, encoded = cv2.imencode(
            ".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), self.jpeg_quality]
        )
        if not ok:
            return False
        path = self.path_for(device_id)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            temporary.write_bytes(encoded.tobytes())
            # Browsers briefly hold the JPEG open on Windows. Retry the
            # atomic swap and drop this frame if the reader keeps the handle;
            # a preview miss must never terminate the camera worker.
            for attempt in range(8):
                try:
                    os.replace(temporary, path)
                    return True
                except PermissionError:
                    if attempt == 7:
                        return False
                    time.sleep(0.02 * (attempt + 1))
        except OSError:
            return False
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass
        return False

    def read(self, device_id: str) -> bytes | None:
        if not self.enabled:
            # The server-side store is read-only and may be constructed with
            # enabled=True; agent-side disabled stores never expose a frame.
            return None
        path = self.path_for(device_id)
        try:
            return path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError:
            return None

    def _fit(self, frame: np.ndarray) -> np.ndarray:
        height, width = frame.shape[:2]
        scale = min(self.max_width / width, self.max_height / height, 1.0)
        if scale == 1.0:
            return frame
        return cv2.resize(
            frame,
            (max(1, round(width * scale)), max(1, round(height * scale))),
            interpolation=cv2.INTER_AREA,
        )
