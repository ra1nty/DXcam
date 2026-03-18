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

import logging
import time
from contextlib import contextmanager
from threading import Lock, current_thread
from typing import Any, Literal, overload

import numpy as np

from dxcam.core import Device, Output, StageSurface
from dxcam.runtime.backend import create_backend_duplicator
from dxcam.runtime.capture_worker import CaptureWorker
from dxcam.runtime.display_recovery import DisplayRecoveryHandler
from dxcam.runtime.frame_buffer import FrameBuffer

from dxcam.core.duplicator_protocol import FrameDuplicator
from dxcam.runtime.output_recovery import OutputRecoveryHandler
from dxcam.processor import Processor
from dxcam.types import CaptureBackend, ColorMode, Frame, ProcessorBackend, Region
from dxcam.util.frame import (
    allocate_output_frame,
    resolve_capture_copy_spec,
    validate_destination_frame,
    validate_region,
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
            max_buffer_len: Kept for API compatibility. Threaded mode uses
                a fixed three-slot latest-only frame buffer.
            backend: Capture backend, ``"dxgi"`` or ``"winrt"``.
            processor_backend: Post-processing backend, ``"cv2"``,
                ``"cython"``, or ``"numpy"``.
        """
        self._is_released = False
        self._output: Output = output
        self._device: Device = device
        self.backend: CaptureBackend = backend
        self._initialize_capture_backend(
            output_color=output_color,
            processor_backend=processor_backend,
        )
        self._initialize_output_state(region=region, output_color=output_color)
        self._initialize_recovery_handler()

        if max_buffer_len != 3:
            logger.info(
                "latest-only three-slot frame buffering is enabled; ignoring "
                "max_buffer_len=%d.",
                max_buffer_len,
            )
        self.max_buffer_len = 3
        self.is_capturing = False

        self.__lock: LockType = Lock()
        self.__worker: CaptureWorker | None = None
        self.__frame_buffer = FrameBuffer(
            slot_count=3,
        )

        self.__last_grab_entry: tuple[Region, Frame] | None = None

    def _initialize_capture_backend(
        self,
        *,
        output_color: ColorMode,
        processor_backend: ProcessorBackend,
    ) -> None:
        self._stagesurf = StageSurface(output=self._output, device=self._device)
        try:
            self._duplicator = self._create_duplicator()
        except Exception:
            self._stagesurf.release()
            raise
        self._processor = Processor(
            output_color=output_color,
            backend=processor_backend,
        )

    def _initialize_output_state(
        self,
        *,
        region: Region | None,
        output_color: ColorMode,
    ) -> None:
        self.width, self.height = self._output.resolution
        self.channel_size = len(output_color) if output_color != "GRAY" else 1
        self.rotation_angle = self._output.rotation_angle
        self._region_set_by_user = region is not None
        self.region = region if region is not None else (0, 0, self.width, self.height)
        validate_region(self.region, self.width, self.height)

    def _initialize_recovery_handler(self) -> None:
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

    def _assert_runtime_mutation_allowed(self) -> None:
        """Allow runtime buffer mutation only on producer thread or when stopped."""
        thread = self.__worker.thread if self.__worker is not None else None
        if thread is not None and thread.is_alive() and current_thread() is not thread:
            raise RuntimeError(
                "Frame buffer mutation requires capture thread to be fully "
                "stopped and joined."
            )

    def _create_duplicator(self) -> FrameDuplicator:
        return create_backend_duplicator(
            self.backend,
            output=self._output,
            device=self._device,
        )

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
        new_frame_only: bool = True,
    ) -> Frame | None:
        """Grab one frame.

        Args:
            region: Optional capture region. Defaults to current camera region.
            new_frame_only: In one-shot mode, return ``None`` when no new frame
                is available. Set ``False`` to reuse the last cached frame for
                the same region.

        Returns:
            Captured frame data or ``None`` when no new frame is available.

        When capture is running (``start()``), this reads from the latest staged
        slot and processes on readout. ``new_frame_only`` does not apply while
        capture is running.

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
            return self._peek_latest_buffered_frame()
        if region is None:
            region = self.region
        else:
            validate_region(region, self.width, self.height)
        frame = self._grab(region, new_frame_only=new_frame_only)
        return frame

    def grab_into(
        self,
        dst: Frame,
        region: Region | None = None,
        new_frame_only: bool = True,
    ) -> bool | None:
        """Grab one frame and write into ``dst``.

        Args:
            dst: Destination ``uint8`` array with the expected frame shape.
            region: Optional capture region. Defaults to current camera region.
            new_frame_only: In one-shot mode, return ``False`` when no new frame
                is available. Set ``False`` to reuse the last cached frame.

        Returns:
            ``True`` when data is written, ``False`` for one-shot no-frame
            result, or ``None`` when capture is stopped and no staged slots exist.

        Example:
            >>> dst = np.empty((720, 1280, 3), dtype=np.uint8)
            >>> ok = cam.grab_into(dst, region=(0, 0, 1280, 720))
        """
        self._ensure_not_released()
        if self.is_capturing:
            if region is not None and region != self.region:
                raise ValueError(
                    "grab_into(region=...) is not supported while capture is running. "
                    "Use start(region=...) to configure capture region."
                )
            with self._read_lease() as leased:
                if leased is None:
                    return None
                validate_destination_frame(
                    dst,
                    frame_width=leased.frame_width,
                    frame_height=leased.frame_height,
                    channel_size=self.channel_size,
                )
                self._process_stage(
                    stage=leased.stage,
                    frame_width=leased.frame_width,
                    frame_height=leased.frame_height,
                    rotation_angle=leased.rotation_angle,
                    dst=dst,
                )
                return True

        if region is None:
            region = self.region
        else:
            validate_region(region, self.width, self.height)
        return self._grab_into(region, dst=dst, new_frame_only=new_frame_only)

    def _peek_latest_buffered_frame(self) -> Frame | None:
        with self._read_lease() as leased:
            if leased is None:
                return None
            return self._process_stage(
                stage=leased.stage,
                frame_width=leased.frame_width,
                frame_height=leased.frame_height,
                rotation_angle=leased.rotation_angle,
            )

    def _wait_for_read_lease(self):
        while True:
            with self.__lock:
                if not self.__frame_buffer.slots:
                    return None
            worker = self.__worker
            if worker is None:
                return None
            if not worker.wait_for_frame(timeout=0.1):
                if not worker.is_running():
                    return None
                continue
            with self.__lock:
                leased = self.__frame_buffer.lease_latest_slot()
                if leased is None:
                    worker.clear_frame_signal()
                    if not self.__frame_buffer.slots:
                        return None
                    if not worker.is_running():
                        return None
                    continue
                worker.clear_frame_signal()
                return leased

    @contextmanager
    def _read_lease(self):
        lease = self._wait_for_read_lease()
        try:
            yield lease
        finally:
            if lease is not None:
                with self.__lock:
                    self.__frame_buffer.release_lease(lease)

    def _process_stage(
        self,
        *,
        stage: StageSurface,
        frame_width: int,
        frame_height: int,
        rotation_angle: int,
        dst: Frame | None = None,
    ) -> Frame:
        if dst is None:
            dst = allocate_output_frame(
                frame_width=frame_width,
                frame_height=frame_height,
                channel_size=self.channel_size,
            )
        with stage.mapped() as rect:
            self._processor.process_into(
                rect,
                frame_width,
                frame_height,
                (0, 0, frame_width, frame_height),
                rotation_angle,
                dst,
            )
        return dst

    def _set_cached_grab_frame(self, region: Region, frame: Frame) -> None:
        self.__last_grab_entry = (region, frame)

    def _get_cached_grab_frame(self, region: Region) -> Frame | None:
        entry = self.__last_grab_entry
        if entry is None:
            return None
        cached_region, cached = entry
        if cached_region != region:
            return None
        return np.array(cached, copy=True)

    def _grab(
        self,
        region: Region,
        new_frame_only: bool = True,
    ) -> Frame | None:
        captured, _frame_ticks, frame_width, frame_height, _rotation_angle = (
            self._capture_to_stage(
                region,
                self._stagesurf,
                wait_for_frame=new_frame_only,
            )
        )
        if not captured:
            if new_frame_only:
                return None
            return self._get_cached_grab_frame(region=region)

        frame = self._process_stage(
            stage=self._stagesurf,
            frame_width=frame_width,
            frame_height=frame_height,
            rotation_angle=self.rotation_angle,
        )
        result = frame
        if not new_frame_only:
            self._set_cached_grab_frame(region=region, frame=result)
        return result

    def _grab_into(
        self,
        region: Region,
        *,
        dst: Frame,
        new_frame_only: bool = True,
    ) -> bool:
        captured, _frame_ticks, frame_width, frame_height, _rotation_angle = (
            self._capture_to_stage(
                region,
                self._stagesurf,
                wait_for_frame=new_frame_only,
            )
        )
        if not captured:
            if new_frame_only:
                return False
            cached = self._get_cached_grab_frame(region=region)
            if cached is None:
                return False
            validate_destination_frame(
                dst,
                frame_width=cached.shape[1],
                frame_height=cached.shape[0],
                channel_size=self.channel_size,
            )
            dst[...] = cached
            return True

        validate_destination_frame(
            dst,
            frame_width=frame_width,
            frame_height=frame_height,
            channel_size=self.channel_size,
        )
        self._process_stage(
            stage=self._stagesurf,
            frame_width=frame_width,
            frame_height=frame_height,
            rotation_angle=self.rotation_angle,
            dst=dst,
        )
        if not new_frame_only:
            self._set_cached_grab_frame(region=region, frame=np.array(dst, copy=True))
        return True

    def _capture_to_stage(
        self,
        region: Region,
        stage: StageSurface,
        *,
        wait_for_frame: bool = True,
    ) -> tuple[bool, int, int, int, int]:
        with self._multithread_guard():
            with self._duplicator.acquire_frame(wait_for_frame=wait_for_frame) as (
                ok,
                updated,
                frame_ticks,
            ):
                if not ok:
                    logger.warning(
                        "Output change/access loss detected (backend=%s, output=%dx%d).",
                        self.backend,
                        self.width,
                        self.height,
                    )
                    self._recover_output()
                    return False, 0, 0, 0, self.rotation_angle
                if not updated:
                    return False, 0, 0, 0, self.rotation_angle
                frame_width, frame_height = self._copy_region_to_surface(region, stage)
        return (
            True,
            frame_ticks,
            frame_width,
            frame_height,
            self.rotation_angle,
        )

    def _copy_region_to_surface(
        self,
        region: Region,
        stage: StageSurface,
    ) -> tuple[int, int]:
        memory_region, frame_width, frame_height, memory_width, memory_height = (
            resolve_capture_copy_spec(
                region,
                self.rotation_angle,
                self._output.surface_size,
            )
        )
        stage.ensure_size(
            dim=(memory_width, memory_height),
        )
        stage.copy_region_from(
            im_context=self._device.im_context,
            src_texture=self._duplicator.texture,
            src_region=memory_region,
        )
        return frame_width, frame_height

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
        with self.__lock:
            self.__frame_buffer.release_stage_slots()

    def _rebuild_recovery_stage_surface(self) -> None:
        self._stagesurf.rebind(output=self._output, device=self._device)
        self._stagesurf.rebuild()

    def _allocate_capture_slots_for_region(
        self,
        region: Region,
        *,
        reason: str,
    ) -> None:
        self._assert_runtime_mutation_allowed()
        _memory_region, frame_width, frame_height, memory_width, memory_height = (
            resolve_capture_copy_spec(
                region,
                self.rotation_angle,
                self._output.surface_size,
            )
        )
        logger.info(
            "Capture slots %s: frame=%dx%d memory=%dx%d c=%d n=%d.",
            reason,
            frame_width,
            frame_height,
            memory_width,
            memory_height,
            self.channel_size,
            self.__frame_buffer.slot_count,
        )
        self.__frame_buffer.allocate_stage_slots(
            output=self._output,
            device=self._device,
            memory_width=memory_width,
            memory_height=memory_height,
            frame_height=frame_height,
            frame_width=frame_width,
            rotation_angle=self.rotation_angle,
        )

    def _get_capture_region(self) -> Region:
        return self.region

    def start(
        self,
        region: Region | None = None,
        target_fps: int = 60,
        video_mode: bool = False,
        delay: int = 0,
    ) -> None:
        """Start threaded capture into latest-only frame-buffer slots.

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
        if self.__worker is not None and self.__worker.is_running():
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
        validate_region(region, self.width, self.height)
        self.is_capturing = True
        self._allocate_capture_slots_for_region(region, reason="build(start)")
        self.__worker = CaptureWorker(
            frame_buffer=self.__frame_buffer,
            lock=self.__lock,
            capture_to_stage=self._capture_to_stage,
            get_region=self._get_capture_region,
            target_fps=target_fps,
            video_mode=video_mode,
            thread_name="DXCamera",
        )
        self.__worker.start()

    def stop(self) -> None:
        """Stop threaded capture and clear buffered frames.

        Example:
            >>> cam.start(target_fps=60)
            >>> _ = cam.get_latest_frame()
            >>> cam.stop()
        """
        worker = self.__worker
        capture_error: Exception | None = None
        elapsed_s = 0.0
        if self.is_capturing:
            if worker is not None:
                worker.stop()
                if not worker.join(timeout=10):
                    raise RuntimeError(
                        "Capture thread did not stop within timeout; refusing "
                        "to clear frame buffer before join."
                    )
                capture_error = worker.consume_error()
                elapsed_s = worker.elapsed_seconds
                if elapsed_s > 0:
                    with self.__lock:
                        frame_count = self.__frame_buffer.frame_count
                    logger.info("Screen Capture FPS: %.4f", frame_count / elapsed_s)
        with self.__lock:
            self._assert_runtime_mutation_allowed()
            self.is_capturing = False
            self.__frame_buffer.clear()
            self.__last_grab_entry = None
        if worker is not None:
            worker.clear_frame_signal()
        self.__worker = None
        if capture_error is not None:
            logger.error("Unhandled exception in capture loop.", exc_info=capture_error)
            raise capture_error

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
            latest_ticks = self.__frame_buffer.latest_frame_ticks
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
            return self.__frame_buffer.latest_frame_ticks

    @overload
    def get_latest_frame(
        self, with_timestamp: Literal[False] = False
    ) -> Frame | None: ...

    @overload
    def get_latest_frame(
        self, with_timestamp: Literal[True] = True
    ) -> tuple[Frame, float] | None: ...

    def get_latest_frame(
        self, with_timestamp: bool = False
    ) -> Frame | tuple[Frame, float] | None:
        """Block until a buffered frame is available and return the latest one.

        Args:
            with_timestamp: Return ``(frame, timestamp_seconds)`` when ``True``.

        Returns:
            Frame data, optionally with timestamp, or ``None`` if capture is
            stopped and the buffer is unavailable.

        Example:
            >>> cam.start(target_fps=60)
            >>> frame, ts = cam.get_latest_frame(with_timestamp=True)
            >>> cam.stop()
        """
        with self._read_lease() as leased:
            if leased is None:
                return None
            frame = self._process_stage(
                stage=leased.stage,
                frame_width=leased.frame_width,
                frame_height=leased.frame_height,
                rotation_angle=leased.rotation_angle,
            )
            if with_timestamp:
                return frame, self._duplicator.ticks_to_seconds(leased.frame_ticks)
            return frame

    @overload
    def get_latest_frame_into(
        self, dst: Frame, with_timestamp: Literal[False] = False
    ) -> bool | None: ...

    @overload
    def get_latest_frame_into(
        self, dst: Frame, with_timestamp: Literal[True] = True
    ) -> tuple[bool, float] | None: ...

    def get_latest_frame_into(
        self,
        dst: Frame,
        with_timestamp: bool = False,
    ) -> bool | tuple[bool, float] | None:
        """Block until a buffered frame is available and write into ``dst``.

        ``dst`` must match the active output shape and channel count.
        """
        with self._read_lease() as leased:
            if leased is None:
                return None
            validate_destination_frame(
                dst,
                frame_width=leased.frame_width,
                frame_height=leased.frame_height,
                channel_size=self.channel_size,
            )
            self._process_stage(
                stage=leased.stage,
                frame_width=leased.frame_width,
                frame_height=leased.frame_height,
                rotation_angle=leased.rotation_angle,
                dst=dst,
            )
            if with_timestamp:
                return True, self._duplicator.ticks_to_seconds(leased.frame_ticks)
            return True

    def _rebuild_frame_buffer(self, region: Region | None) -> None:
        if region is None:
            region = self.region
        with self.__lock:
            self._allocate_capture_slots_for_region(
                region,
                reason="rebuild(output-recovery)",
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
