from __future__ import annotations

import ctypes
import importlib
import logging
import os
from dataclasses import InitVar, dataclass, field
from datetime import timedelta
from threading import Event
from typing import Any, Callable, Literal, cast

import comtypes

from dxcam._libs.d3d11 import ID3D11Multithread, ID3D11Texture2D
from dxcam._libs.dxgi import IDXGIDevice, IDXGISurface
from dxcam.core.device import Device
from dxcam.core.output import Output

logger = logging.getLogger(__name__)
DEFAULT_FRAME_WAIT_SECONDS = 0.002
DEFAULT_MIN_UPDATE_INTERVAL_SECONDS = 0.0
_DIRTY_REGION_DEFAULT = "default"
_DIRTY_REGION_REPORT_ONLY = "report_only"
_DIRTY_REGION_REPORT_AND_RENDER = "report_and_render"
DirtyRegionSetting = Literal[
    "default",
    "report_only",
    "report_and_render",
]


@dataclass(frozen=True)
class _WinRTCaptureBindings:
    frame_pool_cls: Any
    directx_pixel_format: Any
    dirty_region_mode_enum: Any
    create_for_monitor: Callable[[int], Any]
    create_direct3d11_device_from_dxgi_device: Callable[[int], Any]
    get_dxgi_surface_from_object: Callable[[Any], int]

    @classmethod
    def load(cls) -> "_WinRTCaptureBindings":
        try:
            capture_module = importlib.import_module("winrt.windows.graphics.capture")
            capture_interop_module = importlib.import_module(
                "winrt.windows.graphics.capture.interop"
            )
            directx_module = importlib.import_module("winrt.windows.graphics.directx")
            d3d11_interop_module = importlib.import_module(
                "winrt.windows.graphics.directx.direct3d11.interop"
            )
        except ModuleNotFoundError as exc:
            raise ModuleNotFoundError(
                "WinRT backend requires optional dependencies. Install with "
                '`pip install "dxcam[winrt]"`.'
            ) from exc

        Direct3D11CaptureFramePool = getattr(
            capture_module, "Direct3D11CaptureFramePool"
        )
        GraphicsCaptureDirtyRegionMode = getattr(
            capture_module, "GraphicsCaptureDirtyRegionMode"
        )
        create_for_monitor = getattr(capture_interop_module, "create_for_monitor")
        DirectXPixelFormat = getattr(directx_module, "DirectXPixelFormat")
        create_direct3d11_device_from_dxgi_device = getattr(
            d3d11_interop_module, "create_direct3d11_device_from_dxgi_device"
        )
        get_dxgi_surface_from_object = getattr(
            d3d11_interop_module, "get_dxgi_surface_from_object"
        )

        return cls(
            frame_pool_cls=Direct3D11CaptureFramePool,
            directx_pixel_format=DirectXPixelFormat,
            dirty_region_mode_enum=GraphicsCaptureDirtyRegionMode,
            create_for_monitor=create_for_monitor,
            create_direct3d11_device_from_dxgi_device=(
                create_direct3d11_device_from_dxgi_device
            ),
            get_dxgi_surface_from_object=get_dxgi_surface_from_object,
        )


@dataclass
class WinRTDuplicator:
    """Windows.Graphics.Capture wrapper for acquiring frame textures."""

    texture: Any = field(default_factory=lambda: ctypes.POINTER(ID3D11Texture2D)())
    early_release_frame: bool = False
    updated: bool = False
    output: InitVar[Output | None] = None
    device: InitVar[Device | None] = None
    latest_frame_ticks: int = 0
    accumulated_frames: int = 0
    performance_frequency: int = 0

    _output: Output | None = field(default=None, init=False, repr=False)
    _frame: Any | None = field(default=None, init=False, repr=False)
    _frame_pool: Any | None = field(default=None, init=False, repr=False)
    _session: Any | None = field(default=None, init=False, repr=False)
    _capture_item: Any | None = field(default=None, init=False, repr=False)
    _winrt_device: Any | None = field(default=None, init=False, repr=False)
    _multithread: Any | None = field(default=None, init=False, repr=False)
    _dxgi_surface: Any = field(
        default_factory=lambda: ctypes.POINTER(IDXGISurface)(), init=False, repr=False
    )
    _get_dxgi_surface_from_object: Any | None = field(
        default=None, init=False, repr=False
    )
    _frame_arrived_event: Event | None = field(default=None, init=False, repr=False)
    _frame_arrived_token: Any | None = field(default=None, init=False, repr=False)
    _frame_arrived_handler: Any | None = field(default=None, init=False, repr=False)
    _frame_wait_seconds: float = field(default=0.0, init=False, repr=False)
    _frame_pool_size: int = field(default=2, init=False, repr=False)
    _min_update_interval_seconds: float | None = field(
        default=None, init=False, repr=False
    )
    _dirty_region_setting: DirtyRegionSetting = field(
        default=_DIRTY_REGION_DEFAULT, init=False, repr=False
    )
    _cursor_capture_enabled: bool | None = field(default=None, init=False, repr=False)
    _border_required: bool | None = field(default=None, init=False, repr=False)
    _pixel_format: Any | None = field(default=None, init=False, repr=False)
    _dirty_region_mode_enum: Any | None = field(default=None, init=False, repr=False)

    def __post_init__(self, output: Output | None, device: Device | None) -> None:
        if output is None or device is None:
            raise ValueError("WinRTDuplicator requires valid output and device.")
        self._output = output
        self._frame_wait_seconds = self._resolve_frame_wait_seconds()
        self._frame_pool_size = self._resolve_frame_pool_size()
        self._min_update_interval_seconds = self._resolve_min_update_interval_seconds()
        self._dirty_region_setting = self._resolve_dirty_region_setting()
        self._cursor_capture_enabled = self._resolve_bool_env(
            "DXCAM_WINRT_CURSOR_CAPTURE"
        )
        self._border_required = self._resolve_bool_env("DXCAM_WINRT_BORDER_REQUIRED")
        self._configure_qpc_frequency()
        self._configure_multithread_protection(device=device)
        self._create_capture_session(output=output, device=device)

    def _resolve_frame_wait_seconds(self) -> float:
        raw = os.getenv("DXCAM_WINRT_FRAME_WAIT_MS")
        if raw is None:
            return DEFAULT_FRAME_WAIT_SECONDS
        try:
            parsed = float(raw) / 1000.0
        except ValueError:
            logger.warning(
                "Invalid DXCAM_WINRT_FRAME_WAIT_MS=%r; using default %.3fms.",
                raw,
                DEFAULT_FRAME_WAIT_SECONDS * 1000.0,
            )
            return DEFAULT_FRAME_WAIT_SECONDS
        return max(0.0, parsed)

    def _resolve_frame_pool_size(self) -> int:
        raw = os.getenv("DXCAM_WINRT_FRAME_POOL_SIZE")
        if raw is None:
            return 2
        try:
            parsed = int(raw)
        except ValueError:
            logger.warning(
                "Invalid DXCAM_WINRT_FRAME_POOL_SIZE=%r; using default 2.", raw
            )
            return 2
        if parsed < 2:
            logger.warning(
                "DXCAM_WINRT_FRAME_POOL_SIZE=%d is too small; clamping to 2.",
                parsed,
            )
            return 2
        return parsed

    def _resolve_min_update_interval_seconds(self) -> float | None:
        raw = os.getenv("DXCAM_WINRT_MIN_UPDATE_INTERVAL_MS")
        if raw is None:
            return DEFAULT_MIN_UPDATE_INTERVAL_SECONDS
        try:
            parsed = float(raw) / 1000.0
        except ValueError:
            logger.warning(
                "Invalid DXCAM_WINRT_MIN_UPDATE_INTERVAL_MS=%r; ignoring.", raw
            )
            return None
        if parsed < 0:
            logger.warning(
                "DXCAM_WINRT_MIN_UPDATE_INTERVAL_MS=%r is negative; clamping to 0.",
                raw,
            )
            return 0.0
        return parsed

    def _resolve_dirty_region_setting(self) -> DirtyRegionSetting:
        raw = os.getenv("DXCAM_WINRT_DIRTY_REGION_MODE")
        if raw is None:
            return _DIRTY_REGION_DEFAULT
        normalized = raw.strip().lower().replace("-", "_")
        if normalized == _DIRTY_REGION_DEFAULT:
            return _DIRTY_REGION_DEFAULT
        if normalized == _DIRTY_REGION_REPORT_ONLY:
            return _DIRTY_REGION_REPORT_ONLY
        if normalized == _DIRTY_REGION_REPORT_AND_RENDER:
            return _DIRTY_REGION_REPORT_AND_RENDER
        logger.warning(
            "Invalid DXCAM_WINRT_DIRTY_REGION_MODE=%r; using %s.",
            raw,
            _DIRTY_REGION_DEFAULT,
        )
        return _DIRTY_REGION_DEFAULT

    def _resolve_bool_env(self, name: str) -> bool | None:
        raw = os.getenv(name)
        if raw is None:
            return None
        normalized = raw.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
        logger.warning("Invalid %s=%r; ignoring.", name, raw)
        return None

    def _apply_session_options(self) -> None:
        if self._session is None:
            return
        if self._min_update_interval_seconds is not None:
            try:
                self._session.min_update_interval = timedelta(
                    seconds=self._min_update_interval_seconds
                )
            except Exception:
                logger.warning(
                    "Failed to set session min_update_interval.", exc_info=True
                )
        if self._dirty_region_mode_enum is not None and (
            self._dirty_region_setting != _DIRTY_REGION_DEFAULT
        ):
            try:
                if self._dirty_region_setting == _DIRTY_REGION_REPORT_ONLY:
                    mode = self._dirty_region_mode_enum.REPORT_ONLY
                else:
                    mode = self._dirty_region_mode_enum.REPORT_AND_RENDER
                self._session.dirty_region_mode = mode
            except Exception:
                logger.warning("Failed to set session dirty_region_mode.", exc_info=True)
        if self._cursor_capture_enabled is not None:
            try:
                self._session.is_cursor_capture_enabled = self._cursor_capture_enabled
            except Exception:
                logger.warning(
                    "Failed to set session is_cursor_capture_enabled.",
                    exc_info=True,
                )
        if self._border_required is not None:
            try:
                self._session.is_border_required = self._border_required
            except Exception:
                logger.warning("Failed to set session is_border_required.", exc_info=True)

    def _configure_qpc_frequency(self) -> None:
        freq = ctypes.c_longlong()
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.QueryPerformanceFrequency(ctypes.byref(freq))
        self.performance_frequency = int(freq.value)

    def _create_capture_session(self, output: Output, device: Device) -> None:
        bindings = _WinRTCaptureBindings.load()
        self._get_dxgi_surface_from_object = bindings.get_dxgi_surface_from_object
        self._dirty_region_mode_enum = bindings.dirty_region_mode_enum
        self._pixel_format = bindings.directx_pixel_format.B8_G8_R8_A8_UINT_NORMALIZED

        dxgi_device = device.device.QueryInterface(IDXGIDevice)
        dxgi_device_ptr = ctypes.cast(dxgi_device, ctypes.c_void_p).value
        if dxgi_device_ptr is None:
            raise RuntimeError("Failed to get IDXGIDevice pointer for WinRT backend.")
        self._winrt_device = bindings.create_direct3d11_device_from_dxgi_device(
            dxgi_device_ptr
        )

        monitor = self._monitor_handle_to_int(output.hmonitor)
        self._capture_item = bindings.create_for_monitor(monitor)

        self._frame_pool = bindings.frame_pool_cls.create_free_threaded(
            self._winrt_device,
            self._pixel_format,
            self._frame_pool_size,
            self._capture_item.size,
        )
        self._session = self._frame_pool.create_capture_session(self._capture_item)
        self._apply_session_options()
        self._register_frame_arrived_event()
        self._session.start_capture()

    def _register_frame_arrived_event(self) -> None:
        if self._frame_pool is None:
            return
        self._frame_arrived_event = Event()
        self._frame_arrived_handler = self._on_frame_arrived
        try:
            self._frame_arrived_token = self._frame_pool.add_frame_arrived(
                self._frame_arrived_handler
            )
        except Exception:
            logger.debug(
                "Failed to subscribe WinRT frame_arrived event.", exc_info=True
            )
            self._frame_arrived_event = None
            self._frame_arrived_token = None
            self._frame_arrived_handler = None

    def _unregister_frame_arrived_event(self) -> None:
        if self._frame_pool is not None and self._frame_arrived_token is not None:
            try:
                self._frame_pool.remove_frame_arrived(self._frame_arrived_token)
            except Exception:
                logger.debug(
                    "Ignoring exception while removing frame_arrived event.",
                    exc_info=True,
                )
        self._frame_arrived_event = None
        self._frame_arrived_token = None
        self._frame_arrived_handler = None

    def _on_frame_arrived(self, _sender: Any, _args: Any) -> None:
        if self._frame_arrived_event is not None:
            self._frame_arrived_event.set()

    def _configure_multithread_protection(self, device: Device) -> None:
        try:
            self._multithread = device.im_context.QueryInterface(ID3D11Multithread)
        except comtypes.COMError:
            logger.debug("ID3D11Multithread not available for WinRT backend.")
            self._multithread = None
            return
        try:
            self._multithread.SetMultithreadProtected(True)
        except Exception:
            logger.debug("Failed to enable multithread protection.", exc_info=True)

    def enter_multithread(self) -> None:
        if self._multithread is None:
            return
        try:
            self._multithread.Enter()
        except Exception:
            logger.debug("Failed to enter ID3D11Multithread lock.", exc_info=True)

    def leave_multithread(self) -> None:
        if self._multithread is None:
            return
        try:
            self._multithread.Leave()
        except Exception:
            logger.debug("Failed to leave ID3D11Multithread lock.", exc_info=True)

    def _monitor_handle_to_int(self, hmonitor: Any) -> int:
        value = getattr(hmonitor, "value", hmonitor)
        monitor = int(value or 0)
        if monitor == 0:
            raise RuntimeError("Invalid monitor handle for WinRT capture.")
        return monitor

    def _close_winrt_object(self, obj: Any, name: str) -> None:
        if obj is None:
            return
        close_fn = getattr(obj, "close", None)
        if callable(close_fn):
            try:
                close_fn()
            except Exception:
                logger.debug("Ignoring exception while closing %s.", name, exc_info=True)

    def _release_dxgi_surface(self) -> None:
        # The pointer returned by winrt interop behaves like a borrowed pointer in
        # practice. Explicit Release() causes native aborts in stress loops.
        self._dxgi_surface = ctypes.POINTER(IDXGISurface)()

    def _timedelta_to_ticks(self, value: timedelta) -> int:
        if self.performance_frequency <= 0:
            return 0
        return int(value.total_seconds() * self.performance_frequency)

    def _frame_size_mismatch(self, frame: Any) -> bool:
        if self._output is None:
            return False
        expected_width, expected_height = self._output.surface_size
        actual_width = int(frame.content_size.width)
        actual_height = int(frame.content_size.height)
        return (actual_width, actual_height) != (expected_width, expected_height)

    def _try_get_next_frame(self) -> tuple[Any | None, bool]:
        if self._frame_pool is None:
            return None, True
        try:
            return self._frame_pool.try_get_next_frame(), False
        except OSError as exc:
            logger.warning("WinRT frame acquisition failed: %s", exc)
            return None, True

    def _drain_to_latest_frame(self) -> tuple[Any | None, int, bool]:
        latest = None
        drained = 0
        while True:
            frame, failed = self._try_get_next_frame()
            if failed:
                if latest is not None:
                    self._close_winrt_object(latest, "dropped frame")
                return None, drained, True
            if frame is None:
                break
            drained += 1
            if latest is not None:
                self._close_winrt_object(latest, "dropped frame")
            latest = frame
        return latest, drained, False

    def _wait_for_frame_arrival(self) -> bool:
        if self._frame_arrived_event is None or self._frame_wait_seconds <= 0.0:
            return False
        signaled = self._frame_arrived_event.wait(timeout=self._frame_wait_seconds)
        if signaled:
            self._frame_arrived_event.clear()
        return signaled

    def _recreate_frame_pool(self, size: Any) -> bool:
        if (
            self._frame_pool is None
            or self._winrt_device is None
            or self._pixel_format is None
        ):
            return False
        try:
            self._frame_pool.recreate(
                self._winrt_device,
                self._pixel_format,
                self._frame_pool_size,
                size,
            )
        except Exception:
            logger.warning("WinRT frame pool recreate failed.", exc_info=True)
            return False
        return True

    def _handle_frame_size_change(self, frame: Any) -> bool:
        logger.info("WinRT frame size changed; recreating frame pool.")
        content_size = frame.content_size
        self._close_winrt_object(frame, "frame")
        if self._output is not None:
            try:
                self._output.update_desc()
            except Exception:
                logger.debug("Failed to refresh output description.", exc_info=True)
        if not self._recreate_frame_pool(content_size):
            self.updated = False
            return False
        self.updated = False
        self.accumulated_frames = 0
        return True

    def update_frame(self, wait_for_frame: bool = False) -> bool:
        if self._frame is not None or self._dxgi_surface:
            if not self.release_frame():
                self.updated = False
                return False

        frame, drained, failed = self._drain_to_latest_frame()
        if failed:
            self.updated = False
            return False

        if frame is None and wait_for_frame and self._wait_for_frame_arrival():
            frame, extra_drained, failed = self._drain_to_latest_frame()
            drained += extra_drained
            if failed:
                self.updated = False
                return False
        if frame is None:
            self.updated = False
            self.accumulated_frames = 0
            return True
        if self._frame_arrived_event is not None and drained > 0:
            self._frame_arrived_event.clear()

        if self._frame_size_mismatch(frame):
            return self._handle_frame_size_change(frame)

        self._frame = frame
        assert self._get_dxgi_surface_from_object is not None
        surface_ptr = self._get_dxgi_surface_from_object(frame.surface)
        if not surface_ptr:
            self.release_frame()
            self.updated = False
            self.accumulated_frames = 0
            return True

        self._dxgi_surface = ctypes.cast(surface_ptr, ctypes.POINTER(IDXGISurface))
        try:
            self.texture = cast(Any, self._dxgi_surface).QueryInterface(ID3D11Texture2D)
        except comtypes.COMError:
            self.release_frame()
            self.texture = ctypes.POINTER(ID3D11Texture2D)()
            self.updated = False
            self.accumulated_frames = 0
            return True

        frame_ticks = self._timedelta_to_ticks(frame.system_relative_time)
        if frame_ticks > 0:
            self.latest_frame_ticks = frame_ticks
        self.accumulated_frames = max(1, drained)
        self.updated = True
        return True

    @property
    def latest_frame_time(self) -> float:
        return self.ticks_to_seconds(self.latest_frame_ticks)

    def ticks_to_seconds(self, ticks: int) -> float:
        if self.performance_frequency <= 0:
            return 0.0
        return ticks / self.performance_frequency

    def release_frame(self) -> bool:
        if self._frame is not None:
            self._close_winrt_object(self._frame, "frame")
            self._frame = None
        self.texture = ctypes.POINTER(ID3D11Texture2D)()
        self._release_dxgi_surface()
        return True

    def release(self) -> None:
        self.release_frame()
        self._unregister_frame_arrived_event()
        self._close_winrt_object(self._session, "capture session")
        self._close_winrt_object(self._frame_pool, "frame pool")
        self._close_winrt_object(self._capture_item, "capture item")
        self._close_winrt_object(self._winrt_device, "winrt device")
        self._session = None
        self._frame_pool = None
        self._capture_item = None
        self._winrt_device = None

    def __repr__(self) -> str:
        return "<{} Initalized:{}>".format(
            self.__class__.__name__,
            self._session is not None,
        )
