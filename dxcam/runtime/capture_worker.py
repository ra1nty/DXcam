from __future__ import annotations

import time
from _thread import LockType
from dataclasses import dataclass, field
from threading import Event, Thread, current_thread
from typing import Any, Callable

from dxcam.core.stagesurf import StageSurface
from dxcam.runtime.frame_buffer import FrameBuffer
from dxcam.types import Region
from dxcam.util.timer import (
    cancel_timer,
    create_high_resolution_timer,
    set_periodic_timer,
    wait_for_timer,
)

CaptureToStageFn = Callable[[Region, StageSurface], tuple[bool, int, int, int, int]]
GetRegionFn = Callable[[], Region]

__all__ = ["CaptureWorker"]


@dataclass
class CaptureWorker:
    frame_buffer: FrameBuffer
    lock: LockType
    capture_to_stage: CaptureToStageFn
    get_region: GetRegionFn
    target_fps: int = 60
    video_mode: bool = False
    thread_name: str = "DXCamera"
    _frame_available_event: Event = field(default_factory=Event, repr=False)
    _stop_event: Event = field(default_factory=Event, init=False, repr=False)
    _thread: Thread | None = field(default=None, init=False, repr=False)
    _error: Exception | None = field(default=None, init=False, repr=False)
    _elapsed_seconds: float = field(default=0.0, init=False, repr=False)

    @property
    def thread(self) -> Thread | None:
        return self._thread

    @property
    def elapsed_seconds(self) -> float:
        return self._elapsed_seconds

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.is_running():
            raise RuntimeError("Capture worker is already running.")
        self._stop_event.clear()
        self._frame_available_event.clear()
        self._error = None
        self._elapsed_seconds = 0.0
        self._thread = Thread(target=self.run_loop, name=self.thread_name, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._frame_available_event.set()

    def join(self, timeout: float | None = None) -> bool:
        thread = self._thread
        if (
            thread is not None
            and thread.is_alive()
            and thread is not current_thread()
        ):
            thread.join(timeout=timeout)
        thread = self._thread
        if thread is not None and thread.is_alive():
            return False
        self._thread = None
        return True

    def consume_error(self) -> Exception | None:
        error = self._error
        self._error = None
        return error

    def wait_for_frame(self, timeout: float) -> bool:
        return self._frame_available_event.wait(timeout=timeout)

    def clear_frame_signal(self) -> None:
        self._frame_available_event.clear()

    def _run_capture_cycle(self) -> None:
        with self.lock:
            write_slot = self.frame_buffer.reserve_write_slot()
            if write_slot is None:
                if self.video_mode and self.frame_buffer.commit_repeat():
                    self._frame_available_event.set()
                return
            slot_idx, stage = write_slot

        captured, frame_ticks, frame_width, frame_height, rotation_angle = (
            self.capture_to_stage(self.get_region(), stage)
        )
        if not captured:
            if self.video_mode:
                with self.lock:
                    if self.frame_buffer.commit_repeat():
                        self._frame_available_event.set()
            return

        with self.lock:
            if self.frame_buffer.commit_write(
                slot_idx,
                frame_ticks=frame_ticks,
                frame_width=frame_width,
                frame_height=frame_height,
                rotation_angle=rotation_angle,
            ):
                self._frame_available_event.set()

    def run_loop(self) -> None:
        timer_handle: Any | None = None
        if self.target_fps != 0:
            timer_handle = create_high_resolution_timer()
            set_periodic_timer(timer_handle, self.target_fps)

        start_time = time.perf_counter()
        try:
            while not self._stop_event.is_set():
                if timer_handle is not None:
                    wait_for_timer(timer_handle)
                self._run_capture_cycle()
        except Exception as exc:  # pragma: no cover - passthrough path
            self._error = exc
            self._stop_event.set()
            self._frame_available_event.set()
        finally:
            if timer_handle is not None:
                cancel_timer(timer_handle)
            self._elapsed_seconds = time.perf_counter() - start_time
