from __future__ import annotations
from _thread import LockType

import ctypes
import logging
import time
from threading import Event, Lock, Thread, current_thread
from typing import Any, Literal, overload

import comtypes
import numpy as np
from numpy.typing import NDArray

from dxcam._libs.d3d11 import D3D11_BOX
from dxcam.core import Device, Output, StageSurface, Duplicator
from dxcam.processor import Processor
from dxcam.types import ColorMode, Frame, Region
from dxcam.util.timer import (
    create_high_resolution_timer,
    set_periodic_timer,
    wait_for_timer,
    cancel_timer,
)

logger = logging.getLogger(__name__)


class DXCamera:
    """High-level camera interface for one device/output pair."""

    def __init__(
        self,
        output: Output,
        device: Device,
        region: Region | None,
        output_color: ColorMode = "RGB",
        max_buffer_len: int = 8,
    ) -> None:
        self._output: Output = output
        self._device: Device = device
        self._stagesurf: StageSurface = StageSurface(
            output=self._output, device=self._device
        )
        self._duplicator: Duplicator = Duplicator(
            output=self._output, device=self._device
        )
        self._processor: Processor = Processor(output_color=output_color)
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

        self.max_buffer_len = max_buffer_len
        self.is_capturing = False
        self._is_released = False

        self.__thread: Thread | None = None
        self.__lock: LockType = Lock()
        self.__stop_capture = Event()

        self.__frame_available = Event()
        self.__frame_buffer: Frame | None = None
        self.__frame_time_ticks: NDArray[np.int64] | None = None
        self.__head = 0
        self.__tail = 0
        self.__full = False
        self.__has_frame = False

        self.__timer_handle: Any | None = None

        self.__frame_count = 0
        self.__capture_start_time = 0
        self.__latest_frame_ticks: int | None = None

    def grab(self, region: Region | None = None) -> Frame | None:
        self._ensure_not_released()
        if region is None:
            region = self.region
        else:
            self._validate_region(region)
        frame = self._grab(region)
        return frame

    def _grab(self, region: Region) -> Frame | None:
        if not self._duplicator.update_frame():
            self._on_output_change()
            return None
        if not self._duplicator.updated:
            return None

        frame_width, frame_height = self._copy_region_to_stage(region)
        frame = self._process_staging_frame(
            frame_width=frame_width, frame_height=frame_height
        )

        if not self.is_capturing:
            self._duplicator.release_frame()
            return np.array(frame, copy=True)
        return frame

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
        return region[2] - region[0], region[3] - region[1]

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

    def _on_output_change(self) -> None:
        time.sleep(0.1)  # Wait for Display mode change (Access Lost)
        self._duplicator.release()
        self._stagesurf.release()
        self._output.update_desc()
        self.width, self.height = self._output.resolution
        if not self._region_set_by_user:
            self.region = (0, 0, self.width, self.height)
        self._validate_region(self.region)
        if self.is_capturing:
            self._rebuild_frame_buffer(self.region)
        self.rotation_angle = self._output.rotation_angle
        while True:
            try:
                self._stagesurf.rebuild(output=self._output, device=self._device)
                self._duplicator = Duplicator(output=self._output, device=self._device)
            except comtypes.COMError:
                continue
            break

    def start(
        self,
        region: Region | None = None,
        target_fps: int = 60,
        video_mode: bool = False,
        delay: int = 0,
    ) -> None:
        self._ensure_not_released()
        if delay != 0:
            time.sleep(delay)
            self._on_output_change()
        if region is None:
            region = self.region
        self._validate_region(region)
        self.is_capturing = True
        frame_shape = (region[3] - region[1], region[2] - region[0], self.channel_size)
        self.__frame_buffer = np.empty(
            (self.max_buffer_len, *frame_shape), dtype=np.uint8
        )
        self.__frame_time_ticks = np.zeros(self.max_buffer_len, dtype=np.int64)
        self.__head = 0
        self.__tail = 0
        self.__full = False
        self.__has_frame = False
        self.__frame_available.clear()
        self.__thread = Thread(
            target=self.__capture,
            name="DXCamera",
            args=(region, target_fps, video_mode),
        )
        self.__thread.daemon = True
        self.__thread.start()

    def stop(self) -> None:
        if self.is_capturing:
            self.__stop_capture.set()
            self.__frame_available.set()
            if (
                self.__thread is not None
                and self.__thread.is_alive()
                and self.__thread is not current_thread()
            ):
                self.__thread.join(timeout=10)
        with self.__lock:
            self.is_capturing = False
            self.__frame_buffer = None
            self.__frame_time_ticks = None
            self.__frame_count = 0
            self.__latest_frame_ticks = None
            self.__head = 0
            self.__tail = 0
            self.__full = False
            self.__has_frame = False
        self.__frame_available.clear()
        self.__stop_capture.clear()
        self.__thread = None

    @property
    def latest_frame_time(self) -> float | None:
        with self.__lock:
            if self.__latest_frame_ticks is None:
                return None
            return self._duplicator.ticks_to_seconds(self.__latest_frame_ticks)

    @property
    def latest_frame_ticks(self) -> int | None:
        with self.__lock:
            return self.__latest_frame_ticks

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
        while True:
            with self.__lock:
                if self.__frame_buffer is None:
                    return None
            if not self.__frame_available.wait(timeout=0.1):
                continue
            with self.__lock:
                if self.__frame_buffer is None or self.__frame_time_ticks is None:
                    self.__frame_available.clear()
                    return None
                latest_idx = (self.__head - 1) % self.max_buffer_len
                ret = self.__frame_buffer[latest_idx]
                frame_ticks = int(self.__frame_time_ticks[latest_idx])
                self.__frame_available.clear()
            frame = np.array(ret, copy=True) if copy else ret
            if with_timestamp:
                return frame, self._duplicator.ticks_to_seconds(frame_ticks)
            return frame

    @overload
    def get_latest_frame_view(self, with_timestamp: Literal[False] = False) -> Frame | None: ...

    @overload
    def get_latest_frame_view(
        self, with_timestamp: Literal[True] = True
    ) -> tuple[Frame, float] | None: ...

    def get_latest_frame_view(
        self, with_timestamp: bool = False
    ) -> Frame | tuple[Frame, float] | None:
        """Return a zero-copy view into the ring buffer for the latest frame."""
        return self.get_latest_frame(copy=False, with_timestamp=with_timestamp)

    def __capture(
        self,
        region: Region,
        target_fps: int = 60,
        video_mode: bool = False,
    ) -> None:
        if target_fps != 0:
            self.__timer_handle = create_high_resolution_timer()
            set_periodic_timer(self.__timer_handle, target_fps)

        self.__capture_start_time = time.perf_counter()

        capture_error = None

        while not self.__stop_capture.is_set():
            if self.__timer_handle:
                wait_for_timer(self.__timer_handle)
            try:
                frame = self._grab(region)
                if frame is not None:
                    with self.__lock:
                        if self.__frame_buffer is None:
                            continue
                        frame_ticks = self._duplicator.latest_frame_ticks
                        if (
                            frame.shape[0] != self.__frame_buffer.shape[1]
                            or frame.shape[1] != self.__frame_buffer.shape[2]
                        ):
                            logger.info(
                                "Frame size changed from %dx%d to %dx%d; rebuilding frame buffer.",
                                self.__frame_buffer.shape[2],
                                self.__frame_buffer.shape[1],
                                frame.shape[1],
                                frame.shape[0],
                            )
                            self.width, self.height = frame.shape[1], frame.shape[0]
                            if not self._region_set_by_user:
                                self.region = (0, 0, self.width, self.height)
                                region = self.region
                            self._rebuild_frame_buffer_for_shape(
                                frame_height=frame.shape[0],
                                frame_width=frame.shape[1],
                            )
                        self.__frame_buffer[self.__head] = frame
                        if self.__frame_time_ticks is not None:
                            self.__frame_time_ticks[self.__head] = frame_ticks
                        if self.__full:
                            self.__tail = (self.__tail + 1) % self.max_buffer_len
                        self.__head = (self.__head + 1) % self.max_buffer_len
                        self.__latest_frame_ticks = frame_ticks
                        self.__frame_available.set()
                        self.__frame_count += 1
                        self.__full = self.__head == self.__tail
                        self.__has_frame = True
                elif video_mode and self.__has_frame:
                    with self.__lock:
                        if self.__frame_buffer is None or self.__frame_time_ticks is None:
                            continue
                        previous_idx = (self.__head - 1) % self.max_buffer_len
                        np.copyto(
                            self.__frame_buffer[self.__head],
                            self.__frame_buffer[previous_idx],
                        )
                        self.__frame_time_ticks[self.__head] = self.__frame_time_ticks[
                            previous_idx
                        ]
                        if self.__full:
                            self.__tail = (self.__tail + 1) % self.max_buffer_len
                        self.__head = (self.__head + 1) % self.max_buffer_len
                        self.__latest_frame_ticks = int(
                            self.__frame_time_ticks[previous_idx]
                        )
                        self.__frame_available.set()
                        self.__frame_count += 1
                        self.__full = self.__head == self.__tail
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
                self.is_capturing = False
                self.__frame_buffer = None
                self.__frame_time_ticks = None
                self.__latest_frame_ticks = None
                self.__head = 0
                self.__tail = 0
                self.__full = False
                self.__has_frame = False
            self.__frame_available.set()
            self.__stop_capture.clear()
            self.__thread = None
            raise capture_error
        elapsed_s = time.perf_counter() - self.__capture_start_time
        if elapsed_s > 0:
            logger.info("Screen Capture FPS: %.4f", self.__frame_count / elapsed_s)

    def _rebuild_frame_buffer(self, region: Region | None) -> None:
        if region is None:
            region = self.region
        frame_shape = (
            region[3] - region[1],
            region[2] - region[0],
            self.channel_size,
        )
        with self.__lock:
            self.__frame_buffer = np.empty(
                (self.max_buffer_len, *frame_shape), dtype=np.uint8
            )
            self.__frame_time_ticks = np.zeros(self.max_buffer_len, dtype=np.int64)
            self.__head = 0
            self.__tail = 0
            self.__full = False
            self.__has_frame = False

    def _rebuild_frame_buffer_for_shape(
        self, frame_height: int, frame_width: int
    ) -> None:
        frame_shape = (frame_height, frame_width, self.channel_size)
        self.__frame_buffer = np.empty(
            (self.max_buffer_len, *frame_shape), dtype=np.uint8
        )
        self.__frame_time_ticks = np.zeros(self.max_buffer_len, dtype=np.int64)
        self.__head = 0
        self.__tail = 0
        self.__full = False
        self.__has_frame = False

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
        if self._is_released:
            return
        self._is_released = True
        self.stop()
        self._duplicator.release()
        self._stagesurf.release()

    @property
    def is_released(self) -> bool:
        return self._is_released

    def _ensure_not_released(self) -> None:
        if self._is_released:
            raise RuntimeError(
                "DXCamera has been released and cannot be reused. "
                "Create a new camera instance with dxcam.create()."
            )

    def __del__(self) -> None:
        self.release()

    def __repr__(self) -> str:
        return "<{}:\n\t{},\n\t{},\n\t{},\n\t{}\n>".format(
            self.__class__.__name__,
            self._device,
            self._output,
            self._stagesurf,
            self._duplicator,
        )
