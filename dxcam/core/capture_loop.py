from __future__ import annotations

from _thread import LockType
from threading import Event
from typing import Callable

from dxcam.core.capture_runtime import CaptureRuntime
from dxcam.types import Region

CaptureToStageFn = Callable[[Region, int], tuple[bool, int, int, int, int]]


class CaptureLoopRunner:
    """Runs one capture-loop iteration using triple staging slots."""

    def __init__(
        self,
        *,
        lock: LockType,
        frame_available_event: Event,
        runtime: CaptureRuntime,
        capture_to_stage: CaptureToStageFn,
    ) -> None:
        self._lock = lock
        self._frame_available_event = frame_available_event
        self._runtime = runtime
        self._capture_to_stage = capture_to_stage

    def run_once(self, *, region: Region, video_mode: bool) -> None:
        with self._lock:
            write_slot = self._runtime.reserve_write_slot()
            if write_slot is None:
                if video_mode and self._runtime.commit_repeat():
                    self._frame_available_event.set()
                return
            slot_idx, _slot = write_slot

        captured, frame_ticks, frame_width, frame_height, rotation_angle = (
            self._capture_to_stage(region, slot_idx)
        )
        if not captured:
            if video_mode:
                with self._lock:
                    if self._runtime.commit_repeat():
                        self._frame_available_event.set()
            return

        with self._lock:
            if self._runtime.commit_write(
                slot_idx,
                frame_ticks=frame_ticks,
                frame_width=frame_width,
                frame_height=frame_height,
                rotation_angle=rotation_angle,
            ):
                self._frame_available_event.set()
