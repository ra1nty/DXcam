from __future__ import annotations

import time
from dataclasses import dataclass, field

from dxcam.core.device import Device
from dxcam.core.output import Output
from dxcam.core.stagesurf import StageSurface


@dataclass
class CaptureRuntime:
    """Latest-only capture runtime backed by triple staging surfaces.

    Producer writes into one of three StageSurface slots, then publishes the latest
    slot index/timestamp. Consumers lease the latest slot for readout and process on
    demand.
    """

    channel_size: int
    slot_count: int = 3
    slots: list[StageSurface] = field(default_factory=list)
    slot_ticks: list[int] = field(default_factory=list)
    slot_frame_width: list[int] = field(default_factory=list)
    slot_frame_height: list[int] = field(default_factory=list)
    slot_rotation: list[int] = field(default_factory=list)
    slot_readers: list[int] = field(default_factory=list)
    slot_seq: list[int] = field(default_factory=list)
    slot_published_ns: list[int] = field(default_factory=list)
    latest_slot: int = -1
    latest_seq: int = 0
    next_write_slot: int = 0
    has_frame: bool = False
    frame_count: int = 0
    latest_frame_ticks: int | None = None

    def allocate_stage_slots(
        self,
        *,
        output: Output,
        device: Device,
        memory_width: int,
        memory_height: int,
        frame_width: int,
        frame_height: int,
        rotation_angle: int,
    ) -> None:
        self.release_stage_slots()
        self.slots = []
        for _ in range(self.slot_count):
            slot = StageSurface(output=output, device=device)
            if slot.width != memory_width or slot.height != memory_height:
                slot.release()
                slot.rebuild(output=output, device=device, dim=(memory_width, memory_height))
            self.slots.append(slot)
        self.slot_ticks = [0] * self.slot_count
        self.slot_frame_width = [frame_width] * self.slot_count
        self.slot_frame_height = [frame_height] * self.slot_count
        self.slot_rotation = [rotation_angle] * self.slot_count
        self.slot_readers = [0] * self.slot_count
        self.slot_seq = [0] * self.slot_count
        self.slot_published_ns = [0] * self.slot_count
        self.latest_slot = -1
        self.latest_seq = 0
        self.next_write_slot = 0
        self.has_frame = False
        self.frame_count = 0
        self.latest_frame_ticks = None

    def release_stage_slots(self) -> None:
        for slot in self.slots:
            slot.release()
        self.slots = []
        self.slot_ticks = []
        self.slot_frame_width = []
        self.slot_frame_height = []
        self.slot_rotation = []
        self.slot_readers = []
        self.slot_seq = []
        self.slot_published_ns = []

    def clear(self) -> None:
        self.release_stage_slots()
        self.latest_slot = -1
        self.latest_seq = 0
        self.next_write_slot = 0
        self.has_frame = False
        self.frame_count = 0
        self.latest_frame_ticks = None

    def current_frame_shape(self) -> tuple[int, int] | None:
        if not self.has_frame or self.latest_slot < 0:
            return None
        idx = self.latest_slot
        return self.slot_frame_height[idx], self.slot_frame_width[idx]

    def reserve_write_slot(self) -> tuple[int, StageSurface] | None:
        if not self.slots:
            return None
        for offset in range(self.slot_count):
            idx = (self.next_write_slot + offset) % self.slot_count
            if self.slot_readers[idx] != 0:
                continue
            if self.has_frame and idx == self.latest_slot:
                continue
            self.next_write_slot = (idx + 1) % self.slot_count
            return idx, self.slots[idx]
        if self.has_frame and self.latest_slot >= 0 and self.slot_readers[self.latest_slot] == 0:
            idx = self.latest_slot
            self.next_write_slot = (idx + 1) % self.slot_count
            return idx, self.slots[idx]
        return None

    def commit_write(
        self,
        slot_idx: int,
        *,
        frame_ticks: int,
        frame_width: int,
        frame_height: int,
        rotation_angle: int,
    ) -> bool:
        if not (0 <= slot_idx < len(self.slots)):
            return False
        self.latest_seq += 1
        self.slot_ticks[slot_idx] = frame_ticks
        self.slot_frame_width[slot_idx] = frame_width
        self.slot_frame_height[slot_idx] = frame_height
        self.slot_rotation[slot_idx] = rotation_angle
        self.slot_seq[slot_idx] = self.latest_seq
        self.slot_published_ns[slot_idx] = time.perf_counter_ns()
        self.latest_slot = slot_idx
        self.latest_frame_ticks = frame_ticks
        self.frame_count += 1
        self.has_frame = True
        return True

    def commit_repeat(self) -> bool:
        if not self.has_frame:
            return False
        self.frame_count += 1
        return True

    def lease_latest_slot(
        self,
    ) -> tuple[int, StageSurface, int, int, int, int] | None:
        if not self.has_frame or self.latest_slot < 0:
            return None
        idx = self.latest_slot
        self.slot_readers[idx] += 1
        return (
            idx,
            self.slots[idx],
            self.slot_frame_width[idx],
            self.slot_frame_height[idx],
            self.slot_rotation[idx],
            self.slot_ticks[idx],
        )

    def lease_preferred_slot(
        self,
        *,
        min_latest_age_ns: int,
    ) -> tuple[int, StageSurface, int, int, int, int, bool] | None:
        if not self.has_frame or self.latest_slot < 0:
            return None
        latest_idx = self.latest_slot
        preferred_idx = latest_idx
        used_fallback = False
        if min_latest_age_ns > 0:
            latest_age_ns = time.perf_counter_ns() - self.slot_published_ns[latest_idx]
            if latest_age_ns < min_latest_age_ns:
                previous_idx = self._previous_published_slot(latest_idx)
                if previous_idx >= 0:
                    preferred_idx = previous_idx
                    used_fallback = True
        self.slot_readers[preferred_idx] += 1
        return (
            preferred_idx,
            self.slots[preferred_idx],
            self.slot_frame_width[preferred_idx],
            self.slot_frame_height[preferred_idx],
            self.slot_rotation[preferred_idx],
            self.slot_ticks[preferred_idx],
            used_fallback,
        )

    def _previous_published_slot(self, latest_idx: int) -> int:
        latest_seq = self.slot_seq[latest_idx]
        if latest_seq <= 1:
            return -1
        candidate = -1
        candidate_seq = -1
        for idx, seq in enumerate(self.slot_seq):
            if idx == latest_idx:
                continue
            if seq <= 0 or seq >= latest_seq:
                continue
            if seq > candidate_seq:
                candidate = idx
                candidate_seq = seq
        return candidate

    def release_latest_slot(self, slot_idx: int) -> None:
        if not (0 <= slot_idx < len(self.slot_readers)):
            return
        readers = self.slot_readers[slot_idx]
        if readers > 0:
            self.slot_readers[slot_idx] = readers - 1
