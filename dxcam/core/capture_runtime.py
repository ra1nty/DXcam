from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from dxcam.types import Frame


@dataclass
class CaptureRuntime:
    """Ring-buffer runtime state for threaded capture.

    In threaded mode, producer flow is:
    1) reserve slot under lock
    2) copy frame data outside lock
    3) commit metadata under lock

    Callers must ensure frame-buffer clear/realloc never runs concurrently from
    a non-producer thread while producer is active.
    """

    max_buffer_len: int
    channel_size: int
    frame_buffer: Frame | None = None
    frame_time_ticks: NDArray[np.int64] | None = None
    head: int = 0
    tail: int = 0
    full: bool = False
    has_frame: bool = False
    frame_count: int = 0
    latest_frame_ticks: int | None = None

    def allocate_for_shape(self, frame_height: int, frame_width: int) -> None:
        frame_shape = (frame_height, frame_width, self.channel_size)
        self.frame_buffer = np.empty(
            (self.max_buffer_len, *frame_shape),
            dtype=np.uint8,
        )
        self.frame_time_ticks = np.zeros(self.max_buffer_len, dtype=np.int64)
        self.head = 0
        self.tail = 0
        self.full = False
        self.has_frame = False
        self.frame_count = 0
        self.latest_frame_ticks = None

    def clear(self) -> None:
        self.frame_buffer = None
        self.frame_time_ticks = None
        self.head = 0
        self.tail = 0
        self.full = False
        self.has_frame = False
        self.frame_count = 0
        self.latest_frame_ticks = None

    def current_frame_shape(self) -> tuple[int, int] | None:
        if self.frame_buffer is None:
            return None
        return self.frame_buffer.shape[1], self.frame_buffer.shape[2]

    def reserve_write_slot(self) -> tuple[int, Frame] | None:
        if self.frame_buffer is None:
            return None
        write_idx = self.head
        return write_idx, self.frame_buffer[write_idx]

    def reserve_duplicate_copy(
        self,
    ) -> tuple[int, Frame, Frame, int] | None:
        if (
            self.frame_buffer is None
            or self.frame_time_ticks is None
            or not self.has_frame
        ):
            return None
        write_idx = self.head
        previous_idx = (self.head - 1) % self.max_buffer_len
        dst = self.frame_buffer[write_idx]
        src = self.frame_buffer[previous_idx]
        frame_ticks = int(self.frame_time_ticks[previous_idx])
        return write_idx, dst, src, frame_ticks

    def commit_write(self, write_idx: int, frame_ticks: int) -> bool:
        if self.frame_buffer is None or self.frame_time_ticks is None:
            return False
        if write_idx != self.head:
            return False
        if self.full:
            self.tail = (self.tail + 1) % self.max_buffer_len
        self.frame_time_ticks[write_idx] = frame_ticks
        self.head = (write_idx + 1) % self.max_buffer_len
        self.latest_frame_ticks = frame_ticks
        self.frame_count += 1
        self.full = self.head == self.tail
        self.has_frame = True
        return True

    def peek_latest(self, copy: bool = True) -> Frame | None:
        if self.frame_buffer is None or not self.has_frame:
            return None
        latest_idx = (self.head - 1) % self.max_buffer_len
        frame = self.frame_buffer[latest_idx]
        return np.array(frame, copy=True) if copy else frame

    def peek_latest_with_ticks(
        self,
        copy: bool = True,
    ) -> tuple[Frame, int] | None:
        if (
            self.frame_buffer is None
            or self.frame_time_ticks is None
            or not self.has_frame
        ):
            return None
        latest_idx = (self.head - 1) % self.max_buffer_len
        frame = self.frame_buffer[latest_idx]
        frame_ticks = int(self.frame_time_ticks[latest_idx])
        if copy:
            return np.array(frame, copy=True), frame_ticks
        return frame, frame_ticks
