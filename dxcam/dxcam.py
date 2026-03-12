"""DXCamera public class API.

Screenshot usage:
    >>> import dxcam
    >>> with dxcam.create() as cam:
    ...     frame = cam.grab()

Equivalently:
    >>> cam = dxcam.create()
    >>> frame = cam.grab()
    >>> cam.release()

Capture usage:
    >>> import dxcam
    >>> with dxcam.create(output_color="BGR", processor_backend="cv2") as cam:
    ...     cam.start(target_fps=60, video_mode=True)
    ...     frame, ts = cam.get_latest_frame(with_timestamp=True)
    ...     cam.stop()
"""

from __future__ import annotations
from _thread import LockType

import ctypes
import logging
import time
from contextlib import contextmanager
from threading import Event, Lock, Thread, current_thread
from typing import Any, Literal, overload

import numpy as np

from dxcam._libs.d3d11 import D3D11_BOX
from dxcam.core import Device, Output, StageSurface
from dxcam.core.backend import create_backend_duplicator
from dxcam.core.display_recovery import DisplayRecoveryHandler
from dxcam.core.capture_loop import CaptureLoopRunner
from dxcam.core.capture_runtime import CaptureRuntime
from dxcam.core.output_recovery import OutputRecoveryHandler
from dxcam.processor import Processor
from dxcam.types import CaptureBackend, ColorMode, Frame, ProcessorBackend, Region
from dxcam.util.timer import (
    create_high_resolution_timer,
    set_periodic_timer,
    wait_for_timer,
    cancel_timer,
)

logger = logging.getLogger(__name__)

__all__ = ["DXCamera"]


class DXCamera:
    """High-level camera interface for one device/output pair.

    Example:
        >>> import dxcam
        >>> with dxcam.create() as cam:
        ...     frame = cam.grab()
    """

    def __init__(
        self,
        output: Output,
        device: Device,
        region: Region | None,
        output_color: ColorMode = "RGB",
        max_buffer_len: int = 8,
        backend: CaptureBackend = "dxgi",
        processor_backend: ProcessorBackend = "cv2",
    ) -> None:
        """Initialize a camera bound to one output on one device.

        Notes:
            Prefer :func:`dxcam.create` over calling this constructor directly.

        Args:
            output: Internal DXGI output descriptor.
            device: Internal DXGI device descriptor.
            region: Initial capture region as ``(left, top, right, bottom)``.
            output_color: Returned color format.
            max_buffer_len: Ring-buffer size for threaded capture mode.
            backend: Capture backend, ``"dxgi"`` or ``"winrt"``.
            processor_backend: Post-processing backend, ``"cv2"`` or
                ``"numpy"``.
        """
        self._is_released = False
        self._output: Output = output
        self._device: Device = device
        self._stagesurf: StageSurface = StageSurface(
            output=self._output, device=self._device
        )
        self.backend: CaptureBackend = backend
        try:
            self._duplicator: Any = self._create_duplicator()
        except Exception:
            self._stagesurf.release()
            raise
        self._processor: Processor = Processor(
            output_color=output_color,
            backend=processor_backend,
        )
        self._source_region: D3D11_BOX = D3D11_BOX()
        self._source_region.front = 0
        self._source_region.back = 1

        self.width, self.height = self._output.resolution
        self.channel_size = len(output_color) if output_color != "GRAY" else 1
        self.rotation_angle: int = self._output.rotation_angle

        self._region_set_by_user = region is not None
        self.region: Region = (
            region if region is not None else (0, 0, self.width, self.height)
        )
        self._validate_region(self.region)
        self._output_recovery = OutputRecoveryHandler(
            output=self._output,
            device=self._device,
        )
        self._display_recovery = DisplayRecoveryHandler(
            backend=self.backend,
            output_recovery=self._output_recovery,
            release_resources=self._release_recovery_resources,
            rebuild_stage_surface=self._rebuild_recovery_stage_surface,
            create_duplicator=self._create_duplicator,
            rebuild_frame_buffer=self._rebuild_frame_buffer,
            logger=logger,
        )

        if max_buffer_len <= 1:
            logger.warning(
                "max_buffer_len=%d is not supported for concurrent capture; clamping to 2.",
                max_buffer_len,
            )
            self.max_buffer_len = 2
        else:
            self.max_buffer_len = max_buffer_len
        self.is_capturing = False

        self.__thread: Thread | None = None
        self.__lock: LockType = Lock()
        self.__stop_capture = Event()

        self.__frame_available = Event()
        self.__capture_runtime = CaptureRuntime(
            max_buffer_len=self.max_buffer_len,
            channel_size=self.channel_size,
        )

        self.__timer_handle: Any | None = None

        self.__capture_start_time = 0
        self.__last_grab_entry: tuple[Region, Frame] | None = None

    def _assert_runtime_mutation_allowed(self) -> None:
        """Allow runtime buffer mutation only on producer thread or when stopped."""
        thread = self.__thread
        if thread is not None and thread.is_alive() and current_thread() is not thread:
            raise RuntimeError(
                "Frame buffer mutation requires capture thread to be fully "
                "stopped and joined."
            )

    def _create_duplicator(self) -> Any:
        return create_backend_duplicator(
            self.backend,
            output=self._output,
            device=self._device,
        )

    def _uses_early_release(self) -> bool:
        return bool(getattr(self._duplicator, "early_release_frame", True))

    def _release_frame_if_early_release(self) -> None:
        if self._uses_early_release():
            self._duplicator.release_frame()

    def _release_frame_if_late_release(self) -> None:
        if not self._uses_early_release():
            self._duplicator.release_frame()

    def _acquire_new_frame(self, wait_for_frame: bool = False) -> bool:
        if not self._duplicator.update_frame(wait_for_frame=wait_for_frame):
            logger.warning(
                "Output change/access loss detected (backend=%s, output=%dx%d).",
                self.backend,
                self.width,
                self.height,
            )
            self._recover_output()
            return False
        return bool(self._duplicator.updated)

    @contextmanager
    def _multithread_guard(self):
        enter = getattr(self._duplicator, "enter_multithread", None)
        leave = getattr(self._duplicator, "leave_multithread", None)
        if callable(enter) and callable(leave):
            enter()
            try:
                yield
            finally:
                leave()
            return
        yield

    def grab(
        self,
        region: Region | None = None,
        copy: bool = True,
        new_frame_only: bool = True,
    ) -> Frame | None:
        """Grab one frame.

        Args:
            region: Optional capture region. Defaults to current camera region.
            copy: Return caller-owned memory when ``True``. Set ``False`` for
                a reusable internal view.
            new_frame_only: In one-shot mode, return ``None`` when no new frame
                is available. Set ``False`` to reuse the last cached frame for
                the same region.

        Returns:
            Captured frame data or ``None`` when no new frame is available.

        Ownership contract:
        - ``copy=True`` returns caller-owned memory.
        - ``copy=False`` may return internal memory reused by future grabs.
        - ``new_frame_only=True`` returns ``None`` when no new frame is available
          in one-shot mode.
        - ``new_frame_only=False`` falls back to the last successfully grabbed
          frame for the same region in one-shot mode.

        When capture is running (``start()``), this reads from the ring buffer
        instead of touching DXGI objects directly. ``new_frame_only`` does not
        apply while capture is running.

        Example:
            >>> frame = cam.grab(region=(0, 0, 1280, 720), new_frame_only=False)
        """
        self._ensure_not_released()
        if self.is_capturing:
            if region is not None and region != self.region:
                raise ValueError(
                    "grab(region=...) is not supported while capture is running. "
                    "Use start(region=...) to configure capture region."
                )
            return self._peek_latest_buffered_frame(copy=copy)
        if region is None:
            region = self.region
        else:
            self._validate_region(region)
        frame = self._grab(region, copy=copy, new_frame_only=new_frame_only)
        return frame

    def grab_view(self, region: Region | None = None) -> Frame | None:
        """Zero-copy variant of :meth:`grab`.

        Args:
            region: Optional capture region. Defaults to current camera region.

        Returns:
            A non-owning view of frame memory, or ``None`` when unavailable.

        Returns a view backed by internal buffers. Copy the array if you need
        to keep frame data across future capture calls.

        Example:
            >>> frame_view = cam.grab_view()
            >>> frame_owned = frame_view.copy() if frame_view is not None else None
        """
        return self.grab(region=region, copy=False)

    def _peek_latest_buffered_frame(self, copy: bool = True) -> Frame | None:
        with self.__lock:
            return self.__capture_runtime.peek_latest(copy=copy)

    def _set_cached_grab_frame(self, region: Region, frame: Frame) -> None:
        self.__last_grab_entry = (region, frame)

    def _get_cached_grab_frame(self, region: Region, copy: bool = True) -> Frame | None:
        entry = self.__last_grab_entry
        if entry is None:
            return None
        cached_region, cached = entry
        if cached_region != region:
            return None
        return np.array(cached, copy=True) if copy else cached

    def _grab(
        self,
        region: Region,
        copy: bool = True,
        new_frame_only: bool = True,
    ) -> Frame | None:
        if not self._acquire_new_frame(wait_for_frame=new_frame_only):
            if new_frame_only:
                return None
            return self._get_cached_grab_frame(region=region, copy=copy)

        try:
            with self._multithread_guard():
                frame_width, frame_height = self._copy_region_to_stage(region)
                if copy:
                    frame = self._allocate_output_frame(
                        frame_width=frame_width,
                        frame_height=frame_height,
                    )
                    self._process_staging_frame_into(
                        frame_width=frame_width,
                        frame_height=frame_height,
                        dst=frame,
                    )
                else:
                    frame = self._process_staging_frame(
                        frame_width=frame_width, frame_height=frame_height
                    )
        finally:
            self._release_frame_if_late_release()
        result = frame
        if not new_frame_only:
            self._set_cached_grab_frame(region=region, frame=result)
        return result

    def _grab_into(self, region: Region, dst: Frame) -> tuple[bool, int, int, int]:
        """Capture into ``dst`` and return ``(captured, frame_ticks, width, height)``.

        Contract:
        - ``captured`` is True only when ``dst`` has been fully written with a new frame.
        - ``captured`` is False with ``width == height == 0`` when no new frame is available.
        - ``captured`` is False with non-zero ``width``/``height`` when a new frame exists
          but ``dst`` shape does not match, so caller should rebuild and retry.
        """
        if not self._acquire_new_frame(wait_for_frame=True):
            return False, 0, 0, 0

        try:
            with self._multithread_guard():
                frame_width, frame_height = self._copy_region_to_stage(region)
                if frame_height != dst.shape[0] or frame_width != dst.shape[1]:
                    return (
                        False,
                        self._duplicator.latest_frame_ticks,
                        frame_width,
                        frame_height,
                    )
                self._process_staging_frame_into(
                    frame_width=frame_width,
                    frame_height=frame_height,
                    dst=dst,
                )
        finally:
            self._release_frame_if_late_release()
        return True, self._duplicator.latest_frame_ticks, frame_width, frame_height

    def _copy_region_to_stage(self, region: Region) -> tuple[int, int]:
        memory_region = self._region_to_memory_region(region)
        memory_width = memory_region[2] - memory_region[0]
        memory_height = memory_region[3] - memory_region[1]
        if (
            self._stagesurf.width != memory_width
            or self._stagesurf.height != memory_height
        ):
            self._stagesurf.release()
            self._stagesurf.rebuild(
                output=self._output,
                device=self._device,
                dim=(memory_width, memory_height),
            )
        self._update_source_region(memory_region)
        self._device.im_context.CopySubresourceRegion(
            self._stagesurf.texture,
            0,
            0,
            0,
            0,
            self._duplicator.texture,
            0,
            ctypes.byref(self._source_region),
        )
        self._release_frame_if_early_release()  # See remarks in release_frame
        return region[2] - region[0], region[3] - region[1]

    def _allocate_output_frame(self, frame_width: int, frame_height: int) -> Frame:
        return np.empty((frame_height, frame_width, self.channel_size), dtype=np.uint8)

    def _process_staging_frame(self, frame_width: int, frame_height: int) -> Frame:
        rect = self._stagesurf.map()
        try:
            return self._processor.process(
                rect,
                frame_width,
                frame_height,
                (0, 0, frame_width, frame_height),
                self.rotation_angle,
            )
        finally:
            self._stagesurf.unmap()

    def _process_staging_frame_into(
        self,
        frame_width: int,
        frame_height: int,
        dst: Frame,
    ) -> None:
        rect = self._stagesurf.map()
        try:
            self._processor.process_into(
                rect,
                frame_width,
                frame_height,
                (0, 0, frame_width, frame_height),
                self.rotation_angle,
                dst,
            )
        finally:
            self._stagesurf.unmap()

    def _recover_output(self) -> None:
        self.__last_grab_entry = None
        old_width, old_height = self.width, self.height
        old_rotation = self.rotation_angle
        duplicator, output_state = self._display_recovery.handle(
            region=self.region,
            region_set_by_user=self._region_set_by_user,
            is_capturing=self.is_capturing,
        )
        self.width = output_state.width
        self.height = output_state.height
        self.rotation_angle = output_state.rotation_angle
        self.region = output_state.region
        self._duplicator = duplicator
        logger.info(
            "Output recovery: %dx%d@%d -> %dx%d@%d, region=%s.",
            old_width,
            old_height,
            old_rotation,
            self.width,
            self.height,
            self.rotation_angle,
            self.region,
        )

    def _release_recovery_resources(self) -> None:
        self._duplicator.release()
        self._stagesurf.release()

    def _rebuild_recovery_stage_surface(self) -> None:
        self._stagesurf.rebuild(output=self._output, device=self._device)

    def _allocate_frame_buffer_for_shape(
        self,
        frame_height: int,
        frame_width: int,
        *,
        reason: str,
    ) -> None:
        self._assert_runtime_mutation_allowed()
        logger.info(
            "Frame buffer %s: %dx%d c=%d n=%d.",
            reason,
            frame_width,
            frame_height,
            self.channel_size,
            self.max_buffer_len,
        )
        self.__capture_runtime.allocate_for_shape(
            frame_height=frame_height,
            frame_width=frame_width,
        )

    def _handle_frame_size_change(self, frame_height: int, frame_width: int) -> None:
        """Rebuild frame buffers after a source size change.

        Caller must hold ``self.__lock``.
        """
        current_shape = self.__capture_runtime.current_frame_shape()
        assert current_shape is not None
        current_height, current_width = current_shape
        logger.debug(
            "Frame size changed from %dx%d to %dx%d; rebuilding frame buffer.",
            current_width,
            current_height,
            frame_width,
            frame_height,
        )
        self.width, self.height = frame_width, frame_height
        if not self._region_set_by_user:
            self.region = (0, 0, self.width, self.height)
        self._allocate_frame_buffer_for_shape(
            frame_height=frame_height,
            frame_width=frame_width,
            reason="rebuild(frame-size-change)",
        )

    def start(
        self,
        region: Region | None = None,
        target_fps: int = 60,
        video_mode: bool = False,
        delay: int = 0,
    ) -> None:
        """Start threaded capture into the internal ring buffer.

        Args:
            region: Optional region. Defaults to camera region.
            target_fps: Target capture FPS. ``0`` disables timer pacing.
            video_mode: Reuse previous frame when no new frame arrives.
            delay: Optional startup delay in seconds.

        Example:
            >>> cam.start(target_fps=120)
            >>> frame = cam.get_latest_frame()
            >>> cam.stop()
        """
        self._ensure_not_released()
        if self.is_capturing:
            raise RuntimeError("Capture is already running. Call stop() first.")
        if self.__thread is not None and self.__thread.is_alive():
            raise RuntimeError(
                "Capture thread is still alive from previous run. "
                "Call stop() and wait for join before start()."
            )
        if delay != 0:
            time.sleep(delay)
            self._recover_output()
        if region is None:
            region = self.region
        else:
            self._region_set_by_user = True
            self.region = region
        self._validate_region(region)
        self.is_capturing = True
        frame_height = region[3] - region[1]
        frame_width = region[2] - region[0]
        self._allocate_frame_buffer_for_shape(
            frame_height=frame_height,
            frame_width=frame_width,
            reason="build(start)",
        )
        self.__frame_available.clear()
        self.__thread = Thread(
            target=self._capture_loop,
            name="DXCamera",
            args=(target_fps, video_mode),
        )
        self.__thread.daemon = True
        self.__thread.start()

    def stop(self) -> None:
        """Stop threaded capture and clear buffered frames.

        Example:
            >>> cam.start(target_fps=60)
            >>> _ = cam.get_latest_frame()
            >>> cam.stop()
        """
        if self.is_capturing:
            self.__stop_capture.set()
            self.__frame_available.set()
            if (
                self.__thread is not None
                and self.__thread.is_alive()
                and self.__thread is not current_thread()
            ):
                self.__thread.join(timeout=10)
                if self.__thread.is_alive():
                    raise RuntimeError(
                        "Capture thread did not stop within timeout; refusing "
                        "to clear frame buffer before join."
                    )
        with self.__lock:
            self._assert_runtime_mutation_allowed()
            self.is_capturing = False
            self.__capture_runtime.clear()
            self.__last_grab_entry = None
        self.__frame_available.clear()
        self.__stop_capture.clear()
        self.__thread = None

    @property
    def latest_frame_time(self) -> float | None:
        """Timestamp (seconds) of the latest buffered frame, if available.

        Example:
            >>> cam.start(target_fps=60)
            >>> _ = cam.get_latest_frame()
            >>> ts = cam.latest_frame_time
            >>> cam.stop()
        """
        with self.__lock:
            latest_ticks = self.__capture_runtime.latest_frame_ticks
            if latest_ticks is None:
                return None
            return self._duplicator.ticks_to_seconds(latest_ticks)

    @property
    def latest_frame_ticks(self) -> int | None:
        """Raw monotonic backend ticks for the latest buffered frame.

        Example:
            >>> cam.start(target_fps=60)
            >>> _ = cam.get_latest_frame()
            >>> ticks = cam.latest_frame_ticks
            >>> cam.stop()
        """
        with self.__lock:
            return self.__capture_runtime.latest_frame_ticks

    @overload
    def get_latest_frame(
        self, copy: bool = True, with_timestamp: Literal[False] = False
    ) -> Frame | None: ...

    @overload
    def get_latest_frame(
        self, copy: bool = True, with_timestamp: Literal[True] = True
    ) -> tuple[Frame, float] | None: ...

    def get_latest_frame(
        self, copy: bool = True, with_timestamp: bool = False
    ) -> Frame | tuple[Frame, float] | None:
        """Block until a buffered frame is available and return the latest one.

        Args:
            copy: Return a copied array when ``True`` (default). Set to
                ``False`` for a zero-copy view.
            with_timestamp: Return ``(frame, timestamp_seconds)`` when ``True``.

        Returns:
            Frame data, optionally with timestamp, or ``None`` if capture is
            stopped and the buffer is unavailable.

        Example:
            >>> cam.start(target_fps=60)
            >>> frame, ts = cam.get_latest_frame(with_timestamp=True)
            >>> cam.stop()
        """
        while True:
            with self.__lock:
                if self.__capture_runtime.frame_buffer is None:
                    return None
            if not self.__frame_available.wait(timeout=0.1):
                continue
            with self.__lock:
                latest = self.__capture_runtime.peek_latest_with_ticks(copy=copy)
                if latest is None:
                    self.__frame_available.clear()
                    return None
                frame, frame_ticks = latest
                self.__frame_available.clear()
            if with_timestamp:
                return frame, self._duplicator.ticks_to_seconds(frame_ticks)
            return frame

    @overload
    def get_latest_frame_view(
        self, with_timestamp: Literal[False] = False
    ) -> Frame | None: ...

    @overload
    def get_latest_frame_view(
        self, with_timestamp: Literal[True] = True
    ) -> tuple[Frame, float] | None: ...

    def get_latest_frame_view(
        self, with_timestamp: bool = False
    ) -> Frame | tuple[Frame, float] | None:
        """Zero-copy convenience wrapper for :meth:`get_latest_frame`.

        Args:
            with_timestamp: Return ``(frame, timestamp_seconds)`` when ``True``.

        Returns:
            A non-owning frame view, optionally with timestamp.

        Example:
            >>> cam.start(target_fps=60)
            >>> frame_view = cam.get_latest_frame_view()
            >>> cam.stop()
        """
        return self.get_latest_frame(copy=False, with_timestamp=with_timestamp)

    def _capture_loop(
        self,
        target_fps: int = 60,
        video_mode: bool = False,
    ) -> None:
        if target_fps != 0:
            self.__timer_handle = create_high_resolution_timer()
            set_periodic_timer(self.__timer_handle, target_fps)

        self.__capture_start_time = time.perf_counter()

        capture_error = None
        loop_runner = CaptureLoopRunner(
            lock=self.__lock,
            frame_available_event=self.__frame_available,
            runtime=self.__capture_runtime,
            grab_into=self._grab_into,
            process_staging_frame=self._process_staging_frame,
            handle_frame_size_change=self._handle_frame_size_change,
        )

        while not self.__stop_capture.is_set():
            if self.__timer_handle:
                wait_for_timer(self.__timer_handle)
            try:
                loop_runner.run_once(
                    region=self.region,
                    video_mode=video_mode,
                )
            except Exception as e:
                logger.exception("Unhandled exception in capture loop.")
                self.__stop_capture.set()
                capture_error = e
                continue
        if self.__timer_handle:
            cancel_timer(self.__timer_handle)
            self.__timer_handle = None
        if capture_error is not None:
            with self.__lock:
                self._assert_runtime_mutation_allowed()
                self.is_capturing = False
                self.__capture_runtime.clear()
                self.__last_grab_entry = None
            self.__frame_available.set()
            self.__stop_capture.clear()
            self.__thread = None
            raise capture_error
        elapsed_s = time.perf_counter() - self.__capture_start_time
        if elapsed_s > 0:
            with self.__lock:
                frame_count = self.__capture_runtime.frame_count
            logger.info("Screen Capture FPS: %.4f", frame_count / elapsed_s)

    def _rebuild_frame_buffer(self, region: Region | None) -> None:
        if region is None:
            region = self.region
        frame_height = region[3] - region[1]
        frame_width = region[2] - region[0]
        with self.__lock:
            self._allocate_frame_buffer_for_shape(
                frame_height=frame_height,
                frame_width=frame_width,
                reason="rebuild(output-recovery)",
            )

    def _region_to_memory_region(self, region: Region) -> Region:
        if self.rotation_angle == 0:
            return region
        if self.rotation_angle == 90:
            return (
                region[1],
                self._output.surface_size[1] - region[2],
                region[3],
                self._output.surface_size[1] - region[0],
            )
        if self.rotation_angle == 180:
            return (
                self._output.surface_size[0] - region[2],
                self._output.surface_size[1] - region[3],
                self._output.surface_size[0] - region[0],
                self._output.surface_size[1] - region[1],
            )
        if self.rotation_angle == 270:
            return (
                self._output.surface_size[0] - region[3],
                region[0],
                self._output.surface_size[0] - region[1],
                region[2],
            )
        raise ValueError(f"Unsupported rotation angle: {self.rotation_angle}")

    def _update_source_region(self, region: Region) -> None:
        self._source_region.left = region[0]
        self._source_region.top = region[1]
        self._source_region.right = region[2]
        self._source_region.bottom = region[3]

    def _validate_region(self, region: Region) -> None:
        left, top, right, bottom = region
        if not (self.width >= right > left >= 0 and self.height >= bottom > top >= 0):
            raise ValueError(
                f"Invalid Region: Region should be in {self.width}x{self.height}"
            )

    def release(self) -> None:
        """Release resources and permanently invalidate this camera instance.

        Example:
            >>> cam.release()
            >>> cam.is_released
            True
        """
        if self._is_released:
            return
        self._is_released = True
        self.stop()
        self._duplicator.release()
        self._stagesurf.release()

    @property
    def is_released(self) -> bool:
        """Whether :meth:`release` has been called for this camera."""
        return self._is_released

    def _ensure_not_released(self) -> None:
        if self._is_released:
            raise RuntimeError(
                "DXCamera has been released and cannot be reused. "
                "Create a new camera instance with dxcam.create()."
            )

    def __del__(self) -> None:
        try:
            if getattr(self, "_is_released", True):
                return
            self.release()
        except Exception:
            pass

    def __enter__(self) -> "DXCamera":
        self._ensure_not_released()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        self.release()
        return False

    def __repr__(self) -> str:
        return "<{}:\n\t{},\n\t{},\n\t{},\n\t{}\n>".format(
            self.__class__.__name__,
            self._device,
            self._output,
            self._stagesurf,
            self._duplicator,
        )
