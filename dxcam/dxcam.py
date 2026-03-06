from __future__ import annotations

import logging
import time
from threading import Event, Lock, Thread, current_thread
from typing import Any

import comtypes
import numpy as np

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
        max_buffer_len: int = 64,
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

        self.__thread: Thread | None = None
        self.__lock = Lock()
        self.__stop_capture = Event()

        self.__frame_available = Event()
        self.__frame_buffer: Frame | None = None
        self.__head = 0
        self.__tail = 0
        self.__full = False
        self.__has_frame = False

        self.__timer_handle: Any | None = None

        self.__frame_count = 0
        self.__capture_start_time = 0
        self.__latest_frame_time: float | None = None

    def grab(self, region: Region | None = None) -> Frame | None:
        if region is None:
            region = self.region
        self._validate_region(region)
        frame = self._grab(region)
        return frame

    def _grab(self, region: Region) -> Frame | None:
        if self._duplicator.update_frame():
            if not self._duplicator.updated:
                return None
            self._device.im_context.CopyResource(
                self._stagesurf.texture, self._duplicator.texture
            )
            self._duplicator.release_frame()
            rect = self._stagesurf.map()
            frame = self._processor.process(
                rect, self.width, self.height, region, self.rotation_angle
            )
            self._stagesurf.unmap()
            return frame
        else:
            self._on_output_change()
            return None

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
        if delay != 0:
            time.sleep(delay)
            self._on_output_change()
        if region is None:
            region = self.region
        self._validate_region(region)
        self.is_capturing = True
        frame_shape = (region[3] - region[1], region[2] - region[0], self.channel_size)
        self.__frame_buffer = np.zeros(
            (self.max_buffer_len, *frame_shape), dtype=np.uint8
        )
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
            self.__frame_count = 0
            self.__latest_frame_time = None
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
            return self.__latest_frame_time

    def get_latest_frame(self) -> Frame | None:
        while True:
            with self.__lock:
                if self.__frame_buffer is None:
                    return None
            if not self.__frame_available.wait(timeout=0.1):
                continue
            with self.__lock:
                if self.__frame_buffer is None:
                    self.__frame_available.clear()
                    return None
                ret = self.__frame_buffer[(self.__head - 1) % self.max_buffer_len]
                self.__frame_available.clear()
            return np.array(ret)

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
                        self.__frame_buffer[self.__head] = frame
                        if self.__full:
                            self.__tail = (self.__tail + 1) % self.max_buffer_len
                        self.__head = (self.__head + 1) % self.max_buffer_len
                        self.__latest_frame_time = self._duplicator.latest_frame_time
                        self.__frame_available.set()
                        self.__frame_count += 1
                        self.__full = self.__head == self.__tail
                        self.__has_frame = True
                elif video_mode and self.__has_frame:
                    with self.__lock:
                        if self.__frame_buffer is None:
                            continue
                        self.__frame_buffer[self.__head] = np.array(
                            self.__frame_buffer[(self.__head - 1) % self.max_buffer_len]
                        )
                        if self.__full:
                            self.__tail = (self.__tail + 1) % self.max_buffer_len
                        self.__head = (self.__head + 1) % self.max_buffer_len
                        self.__latest_frame_time = self._duplicator.latest_frame_time
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
                self.__latest_frame_time = None
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
            logger.info("Screen Capture FPS: %d", int(self.__frame_count / elapsed_s))

    def _rebuild_frame_buffer(self, region: Region | None) -> None:
        if region is None:
            region = self.region
        frame_shape = (
            region[3] - region[1],
            region[2] - region[0],
            self.channel_size,
        )
        with self.__lock:
            self.__frame_buffer = np.zeros(
                (self.max_buffer_len, *frame_shape), dtype=np.uint8
            )
            self.__head = 0
            self.__tail = 0
            self.__full = False
            self.__has_frame = False

    def _validate_region(self, region: Region) -> None:
        left, top, right, bottom = region
        if not (
            self.width >= right > left >= 0 and self.height >= bottom > top >= 0
        ):
            raise ValueError(
                f"Invalid Region: Region should be in {self.width}x{self.height}"
            )

    def release(self) -> None:
        self.stop()
        self._duplicator.release()
        self._stagesurf.release()

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
