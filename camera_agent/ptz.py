"""Published-ONVIF PTZ adapter used by manual control and local patrol.

No vendor P2P protocol is attempted here.  ONVIF support varies by camera, so
the adapter only connects after the manifest explicitly enables it and fails
closed if the camera has no PTZ media profile.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import Enum
import importlib
import inspect
import logging
import threading
import time


LOGGER = logging.getLogger(__name__)


class PTZError(RuntimeError):
    pass


class PTZMove(str, Enum):
    LEFT = "left"
    RIGHT = "right"
    UP = "up"
    DOWN = "down"


_VELOCITIES: dict[PTZMove, tuple[float, float]] = {
    PTZMove.LEFT: (-1.0, 0.0),
    PTZMove.RIGHT: (1.0, 0.0),
    PTZMove.UP: (0.0, 1.0),
    PTZMove.DOWN: (0.0, -1.0),
}


def _resolve_onvif(value: object) -> object:
    """Accept either the maintained async client or a compatible sync client."""
    return asyncio.run(value) if inspect.isawaitable(value) else value


class PTZController:
    @property
    def available(self) -> bool:
        return False

    def move(self, direction: PTZMove, *, duration_seconds: float | None = None) -> None:
        raise PTZError("PTZ is not enabled for this agent")

    def start(self, direction: PTZMove, *, duration_seconds: float | None = None) -> None:
        """Start a bounded move without blocking the camera/inference loop.

        Basic adapters retain the old synchronous behaviour.  ONVIF overrides
        this with a small worker so control commands and detection can continue
        while the camera is moving.
        """
        self.move(direction, duration_seconds=duration_seconds)

    def stop(self) -> None:
        return None


class NoopPTZ(PTZController):
    pass


class PTZArbiter:
    """Serialize every PTZ owner for one device.

    ONVIF motion already cancels a previous segment, but feature switching can
    race a patrol worker and a manual command.  Giving all callers one owner
    gate means an old feature cannot issue a late move after it has been
    disabled.
    """

    def __init__(self, controller: PTZController) -> None:
        self.controller = controller
        self._owner: str | None = None
        self._lock = threading.RLock()

    @property
    def available(self) -> bool:
        return self.controller.available

    @property
    def owner(self) -> str | None:
        with self._lock:
            return self._owner

    def start(
        self,
        owner: str,
        direction: PTZMove,
        *,
        duration_seconds: float | None = None,
    ) -> None:
        if not owner:
            raise PTZError("PTZ owner is required")
        with self._lock:
            # Stop before changing ownership even when the underlying adapter
            # still believes a previous non-blocking segment is in flight.
            if self._owner is not None:
                self.controller.stop()
            self.controller.start(direction, duration_seconds=duration_seconds)
            self._owner = owner

    def stop(self, owner: str | None = None, *, force: bool = False) -> bool:
        with self._lock:
            if not force and owner is not None and self._owner != owner:
                return False
            self.controller.stop()
            self._owner = None
            return True


@dataclass
class OnvifPTZ(PTZController):
    host: str
    port: int
    username: str = field(repr=False)
    password: str = field(repr=False)
    profile_token: str | None = None
    velocity: float = 0.35
    move_duration_seconds: float = 0.7
    _client: object | None = field(default=None, init=False, repr=False)
    _service: object | None = field(default=None, init=False, repr=False)
    _profile_token: str | None = field(default=None, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _motion_guard: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _motion_thread: threading.Thread | None = field(default=None, init=False, repr=False)
    _motion_cancel: threading.Event | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if not self.host:
            raise PTZError("ONVIF PTZ host is required")
        if not 1 <= self.port <= 65535:
            raise PTZError("ONVIF PTZ port must be between 1 and 65535")
        if not self.username or not self.password:
            raise PTZError("ONVIF PTZ username and password are required")
        if not 0 < self.velocity <= 1:
            raise PTZError("ONVIF PTZ velocity must be between 0 and 1")
        if not 0.1 <= self.move_duration_seconds <= 5:
            raise PTZError("ONVIF PTZ move duration must be between 0.1 and 5")

    @property
    def available(self) -> bool:
        return True

    def probe(self) -> str:
        """Verify ONVIF discovery and a PTZ media profile without moving camera."""
        with self._lock:
            if self._uses_async_client():
                try:
                    return asyncio.run(self._async_probe())
                except PTZError:
                    raise
                except Exception as exc:
                    raise PTZError(f"ONVIF PTZ connection failed: {type(exc).__name__}") from exc
            _service, token = self._connect_locked()
            return token

    def _camera_type(self) -> object:
        try:
            module = importlib.import_module("onvif")
            return getattr(module, "ONVIFCamera")
        except (ImportError, AttributeError) as exc:
            raise PTZError(
                "ONVIF PTZ needs optional package 'onvif-zeep-async'. Install it before enabling ptz."
            ) from exc

    def _uses_async_client(self) -> bool:
        camera_type = self._camera_type()
        return inspect.iscoroutinefunction(getattr(camera_type, "create_media_service", None))

    def _connect_locked(self) -> tuple[object, str]:
        if self._service is not None and self._profile_token is not None:
            return self._service, self._profile_token
        try:
            camera_type = self._camera_type()
            client = camera_type(self.host, self.port, self.username, self.password)
            media = client.create_media_service()
            profiles = media.GetProfiles()
            profile = next(
                (
                    item
                    for item in profiles
                    if getattr(item, "token", None) == self.profile_token
                    and getattr(item, "PTZConfiguration", None) is not None
                ),
                None,
            ) if self.profile_token else next(
                (item for item in profiles if getattr(item, "PTZConfiguration", None) is not None),
                None,
            )
            if profile is None or not getattr(profile, "token", None):
                raise PTZError("ONVIF camera has no PTZ-capable media profile")
            self._client = client
            self._service = client.create_ptz_service()
            self._profile_token = str(profile.token)
            return self._service, self._profile_token
        except PTZError:
            raise
        except Exception as exc:
            raise PTZError(f"ONVIF PTZ connection failed: {type(exc).__name__}") from exc

    async def _async_connect(self) -> tuple[object, object, str]:
        camera_type = self._camera_type()
        client = camera_type(self.host, self.port, self.username, self.password)
        await client.update_xaddrs()
        media = await client.create_media_service()
        profiles = await media.GetProfiles()
        profile = next(
            (
                item
                for item in profiles
                if getattr(item, "token", None) == self.profile_token
                and getattr(item, "PTZConfiguration", None) is not None
            ),
            None,
        ) if self.profile_token else next(
            (item for item in profiles if getattr(item, "PTZConfiguration", None) is not None),
            None,
        )
        if profile is None or not getattr(profile, "token", None):
            close = getattr(client, "close", None)
            if callable(close):
                await close()
            raise PTZError("ONVIF camera has no PTZ-capable media profile")
        return client, await client.create_ptz_service(), str(profile.token)

    async def _async_probe(self) -> str:
        client, _service, token = await self._async_connect()
        close = getattr(client, "close", None)
        if callable(close):
            await close()
        return token

    async def _async_move(
        self,
        direction: PTZMove,
        duration: float,
        cancel_event: threading.Event | None = None,
    ) -> None:
        client, service, token = await self._async_connect()
        raw_x, raw_y = _VELOCITIES[direction]
        velocity = {"PanTilt": {"x": raw_x * self.velocity, "y": raw_y * self.velocity}}
        try:
            await service.ContinuousMove({"ProfileToken": token, "Velocity": velocity})
            # Poll a threading.Event so STOP can interrupt a long, smooth move
            # without waiting for the configured segment duration to elapse.
            elapsed = 0.0
            while elapsed < duration and not (cancel_event and cancel_event.is_set()):
                step = min(0.05, duration - elapsed)
                await asyncio.sleep(step)
                elapsed += step
        finally:
            try:
                await service.Stop({"ProfileToken": token, "PanTilt": True, "Zoom": True})
            finally:
                close = getattr(client, "close", None)
                if callable(close):
                    await close()

    async def _async_stop(self) -> None:
        client, service, token = await self._async_connect()
        try:
            await service.Stop({"ProfileToken": token, "PanTilt": True, "Zoom": True})
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                await close()

    def move(self, direction: PTZMove, *, duration_seconds: float | None = None) -> None:
        if not isinstance(direction, PTZMove):
            raise PTZError("unsupported PTZ direction")
        duration = self.move_duration_seconds if duration_seconds is None else duration_seconds
        if not 0.1 <= duration <= 5:
            raise PTZError("ONVIF PTZ move duration must be between 0.1 and 5")
        raw_x, raw_y = _VELOCITIES[direction]
        velocity = {"PanTilt": {"x": raw_x * self.velocity, "y": raw_y * self.velocity}}
        with self._lock:
            if self._uses_async_client():
                try:
                    asyncio.run(self._async_move(direction, duration))
                except Exception as exc:
                    raise PTZError(f"ONVIF PTZ move failed: {type(exc).__name__}") from exc
                return
            service, token = self._connect_locked()
            try:
                _resolve_onvif(service.ContinuousMove({"ProfileToken": token, "Velocity": velocity}))
                time.sleep(duration)
            except Exception as exc:
                self._service = None
                self._profile_token = None
                raise PTZError(f"ONVIF PTZ move failed: {type(exc).__name__}") from exc
            finally:
                try:
                    _resolve_onvif(service.Stop({"ProfileToken": token, "PanTilt": True, "Zoom": True}))
                except Exception as exc:
                    LOGGER.error("ONVIF PTZ stop failed: %s", type(exc).__name__)

    def _move_worker(
        self,
        direction: PTZMove,
        duration: float,
        cancel_event: threading.Event,
    ) -> None:
        try:
            raw_x, raw_y = _VELOCITIES[direction]
            velocity = {"PanTilt": {"x": raw_x * self.velocity, "y": raw_y * self.velocity}}
            with self._lock:
                if self._uses_async_client():
                    try:
                        asyncio.run(self._async_move(direction, duration, cancel_event))
                    except Exception as exc:
                        raise PTZError(f"ONVIF PTZ move failed: {type(exc).__name__}") from exc
                    return
                service, token = self._connect_locked()
                try:
                    LOGGER.debug("ONVIF PTZ ContinuousMove issued")
                    _resolve_onvif(service.ContinuousMove({"ProfileToken": token, "Velocity": velocity}))
                    deadline = time.monotonic() + duration
                    while time.monotonic() < deadline and not cancel_event.is_set():
                        cancel_event.wait(min(0.05, max(0.0, deadline - time.monotonic())))
                finally:
                    try:
                        _resolve_onvif(service.Stop({"ProfileToken": token, "PanTilt": True, "Zoom": True}))
                    except Exception as exc:
                        LOGGER.error("ONVIF PTZ stop failed: %s", type(exc).__name__)
        except PTZError as exc:
            LOGGER.error("ONVIF PTZ background move failed: %s", exc)
        except Exception as exc:
            LOGGER.error("ONVIF PTZ background move failed: %s", type(exc).__name__)
        finally:
            with self._motion_guard:
                if self._motion_thread is threading.current_thread():
                    self._motion_thread = None
                    self._motion_cancel = None

    def start(self, direction: PTZMove, *, duration_seconds: float | None = None) -> None:
        if not isinstance(direction, PTZMove):
            raise PTZError("unsupported PTZ direction")
        duration = self.move_duration_seconds if duration_seconds is None else duration_seconds
        if not 0.1 <= duration <= 5:
            raise PTZError("ONVIF PTZ move duration must be between 0.1 and 5")
        with self._motion_guard:
            old_thread = self._motion_thread
            old_cancel = self._motion_cancel
            if old_cancel is not None:
                old_cancel.set()
        if old_thread is not None and old_thread is not threading.current_thread():
            old_thread.join(timeout=2.0)
            if old_thread.is_alive():
                raise PTZError("previous ONVIF PTZ move did not stop")
        cancel_event = threading.Event()
        thread = threading.Thread(
            target=self._move_worker,
            args=(direction, duration, cancel_event),
            name="onvif-ptz-motion",
            daemon=True,
        )
        with self._motion_guard:
            self._motion_cancel = cancel_event
            self._motion_thread = thread
        LOGGER.info("ONVIF PTZ move started: %s for %.1fs", direction.value, duration)
        thread.start()

    def stop(self) -> None:
        with self._motion_guard:
            motion_thread = self._motion_thread
            cancel_event = self._motion_cancel
            if cancel_event is not None:
                cancel_event.set()
        if motion_thread is not None and motion_thread is not threading.current_thread():
            motion_thread.join(timeout=2.0)
            if motion_thread.is_alive():
                raise PTZError("ONVIF PTZ move did not stop")
            return
        with self._lock:
            if self._uses_async_client():
                try:
                    asyncio.run(self._async_stop())
                except Exception as exc:
                    raise PTZError(f"ONVIF PTZ stop failed: {type(exc).__name__}") from exc
                return
            try:
                service, token = self._connect_locked()
                _resolve_onvif(service.Stop({"ProfileToken": token, "PanTilt": True, "Zoom": True}))
            except PTZError:
                raise
            except Exception as exc:
                self._service = None
                self._profile_token = None
                raise PTZError(f"ONVIF PTZ stop failed: {type(exc).__name__}") from exc
