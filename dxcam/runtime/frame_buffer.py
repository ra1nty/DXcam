from __future__ import annotations

from dataclasses import dataclass, field

from dxcam.core.device import Device
from dxcam.core.output import Output
from dxcam.core.stagesurf import StageSurface

__all__ = ["LeasedFrameSlot", "FrameBuffer"]


@dataclass(frozen=True)
class LeasedFrameSlot:
    """Immutable snapshot of a leased frame slot for readout."""

    slot_idx: int
    stage: StageSurface
    frame_width: int
    frame_height: int
    rotation_angle: int
    frame_ticks: int


@dataclass
class FrameBuffer:
    """Latest-only capture state backed by three staging surfaces."""

    slot_count: int = 3
    slots: list[StageSurface] = field(default_factory=list)
    slot_ticks: list[int] = field(default_factory=list)
    slot_frame_width: list[int] = field(default_factory=list)
    slot_frame_height: list[int] = field(default_factory=list)
    slot_rotation: list[int] = field(default_factory=list)
    slot_readers: list[int] = field(default_factory=list)
    latest_slot: int = -1
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
        self.slots = [
            StageSurface(output=output, device=device, dim=(memory_width, memory_height))
            for _ in range(self.slot_count)
        ]
        self.slot_ticks = [0] * self.slot_count
        self.slot_frame_width = [frame_width] * self.slot_count
        self.slot_frame_height = [frame_height] * self.slot_count
        self.slot_rotation = [rotation_angle] * self.slot_count
        self.slot_readers = [0] * self.slot_count
        self.latest_slot = -1
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

    def clear(self) -> None:
        self.release_stage_slots()
        self.latest_slot = -1
        self.next_write_slot = 0
        self.has_frame = False
        self.frame_count = 0
        self.latest_frame_ticks = None

    def _build_lease(self, idx: int) -> LeasedFrameSlot:
        return LeasedFrameSlot(
            slot_idx=idx,
            stage=self.slots[idx],
            frame_width=self.slot_frame_width[idx],
            frame_height=self.slot_frame_height[idx],
            rotation_angle=self.slot_rotation[idx],
            frame_ticks=self.slot_ticks[idx],
        )

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
        if (
            self.has_frame
            and self.latest_slot >= 0
            and self.slot_readers[self.latest_slot] == 0
        ):
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
        self.slot_ticks[slot_idx] = frame_ticks
        self.slot_frame_width[slot_idx] = frame_width
        self.slot_frame_height[slot_idx] = frame_height
        self.slot_rotation[slot_idx] = rotation_angle
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

    def lease_latest_slot(self) -> LeasedFrameSlot | None:
        if not self.has_frame or self.latest_slot < 0:
            return None
        idx = self.latest_slot
        self.slot_readers[idx] += 1
        return self._build_lease(idx)

    def release_latest_slot(self, slot_idx: int) -> None:
        if not (0 <= slot_idx < len(self.slot_readers)):
            return
        readers = self.slot_readers[slot_idx]
        if readers > 0:
            self.slot_readers[slot_idx] = readers - 1

    def release_lease(self, lease: LeasedFrameSlot) -> None:
        self.release_latest_slot(lease.slot_idx)
