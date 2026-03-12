from __future__ import annotations

from _thread import LockType
from threading import Event
from typing import Callable

import numpy as np

from dxcam.core.capture_runtime import CaptureRuntime
from dxcam.types import Frame, Region

GrabIntoFn = Callable[[Region, Frame], tuple[bool, int, int, int]]
ProcessStagingFrameFn = Callable[[int, int], Frame]
HandleFrameSizeChangeFn = Callable[[int, int], None]


class CaptureLoopRunner:
    """Runs one capture-loop iteration and updates the ring buffer."""

    def __init__(
        self,
        *,
        lock: LockType,
        frame_available_event: Event,
        runtime: CaptureRuntime,
        grab_into: GrabIntoFn,
        process_staging_frame: ProcessStagingFrameFn,
        handle_frame_size_change: HandleFrameSizeChangeFn,
    ) -> None:
        self._lock = lock
        self._frame_available_event = frame_available_event
        self._runtime = runtime
        self._grab_into = grab_into
        self._process_staging_frame = process_staging_frame
        self._handle_frame_size_change = handle_frame_size_change

    def run_once(self, *, region: Region, video_mode: bool) -> None:
        with self._lock:
            write_slot = self._runtime.reserve_write_slot()
            if write_slot is None:
                return
            write_idx, write_dst = write_slot

        captured, frame_ticks, frame_width, frame_height = self._grab_into(
            region,
            write_dst,
        )
        if captured:
            with self._lock:
                if self._runtime.commit_write(write_idx, frame_ticks):
                    self._frame_available_event.set()
            return

        if frame_width > 0 and frame_height > 0:
            frame = self._process_staging_frame(frame_width, frame_height)
            with self._lock:
                current_shape = self._runtime.current_frame_shape()
                if current_shape is None:
                    return
                current_height, current_width = current_shape
                if frame.shape[0] != current_height or frame.shape[1] != current_width:
                    self._handle_frame_size_change(frame.shape[0], frame.shape[1])

                write_slot = self._runtime.reserve_write_slot()
                if write_slot is None:
                    return
                write_idx, write_dst = write_slot

            np.copyto(write_dst, frame)
            with self._lock:
                if self._runtime.commit_write(write_idx, frame_ticks):
                    self._frame_available_event.set()
            return

        if video_mode:
            with self._lock:
                duplicate_copy = self._runtime.reserve_duplicate_copy()
            if duplicate_copy is None:
                return
            write_idx, write_dst, previous_dst, frame_ticks = duplicate_copy
            np.copyto(write_dst, previous_dst)
            with self._lock:
                if self._runtime.commit_write(write_idx, frame_ticks):
                    self._frame_available_event.set()
        return
