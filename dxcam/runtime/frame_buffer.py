from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from dxcam.core.stagesurf import StageSurface

__all__ = ["FrameSlot", "LeasedFrameSlot", "FrameBuffer"]


@dataclass(eq=False)
class FrameSlot:
    """A staging surface and the owners that keep it alive."""

    stage: StageSurface
    frame_width: int
    frame_height: int
    rotation_angle: int
    frame_ticks: int = 0
    readers: int = 0
    writing: bool = False
    retired: bool = False
    _released: bool = field(default=False, init=False, repr=False)


@dataclass(frozen=True)
class LeasedFrameSlot:
    """Immutable frame metadata tied to the exact surface being read."""

    slot: FrameSlot
    frame_width: int
    frame_height: int
    rotation_angle: int
    frame_ticks: int
    _released: bool = field(default=False, init=False, repr=False, compare=False)

    @property
    def stage(self) -> StageSurface:
        return self.slot.stage


@dataclass
class FrameBuffer:
    """Latest-only publication with three staging surfaces.

    Callers must serialize all methods with the frame lock. A reservation keeps
    its surface alive until commit or cancellation; a lease does so until release.
    Retiring a generation never invalidates either kind of outstanding owner.
    """

    slot_count: ClassVar[int] = 3
    slots: list[FrameSlot] = field(default_factory=list)
    latest_slot: FrameSlot | None = None
    next_write_slot: int = 0
    has_frame: bool = False
    frame_count: int = 0
    latest_frame_ticks: int | None = None

    def replace_slots(
        self,
        stages: list[StageSurface],
        *,
        frame_width: int,
        frame_height: int,
        rotation_angle: int,
    ) -> None:
        """Take ownership of three newly allocated staging surfaces."""
        if len(stages) != self.slot_count:
            raise ValueError(
                f"FrameBuffer requires {self.slot_count} staging surfaces."
            )
        slots = [
            FrameSlot(stage, frame_width, frame_height, rotation_angle)
            for stage in stages
        ]
        self.clear()
        self.slots = slots

    @staticmethod
    def _release_retired_slot(slot: FrameSlot) -> None:
        if (
            slot.retired
            and not slot.writing
            and slot.readers == 0
            and not slot._released
        ):
            slot._released = True
            slot.stage.release()

    def release_stage_slots(self) -> None:
        slots, self.slots = self.slots, []
        self.latest_slot = None
        self.next_write_slot = 0
        self.has_frame = False
        self.frame_count = 0
        self.latest_frame_ticks = None
        for slot in slots:
            slot.retired = True
            self._release_retired_slot(slot)

    def clear(self) -> None:
        self.release_stage_slots()

    def reserve_write_slot(self) -> FrameSlot | None:
        for offset in range(len(self.slots)):
            idx = (self.next_write_slot + offset) % len(self.slots)
            slot = self.slots[idx]
            if slot is self.latest_slot or slot.readers or slot.writing:
                continue
            slot.writing = True
            self.next_write_slot = (idx + 1) % len(self.slots)
            return slot
        return None

    def cancel_write(self, slot: FrameSlot) -> None:
        """Finish a reservation without publishing, including retired writes."""
        slot.writing = False
        self._release_retired_slot(slot)

    def commit_write(
        self,
        slot: FrameSlot,
        *,
        frame_ticks: int,
        frame_width: int,
        frame_height: int,
        rotation_angle: int,
    ) -> bool:
        if slot.retired or not slot.writing or slot not in self.slots:
            return False
        slot.frame_ticks = frame_ticks
        slot.frame_width = frame_width
        slot.frame_height = frame_height
        slot.rotation_angle = rotation_angle
        slot.writing = False
        self.latest_slot = slot
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
        slot = self.latest_slot
        if slot is None:
            return None
        slot.readers += 1
        return LeasedFrameSlot(
            slot=slot,
            frame_width=slot.frame_width,
            frame_height=slot.frame_height,
            rotation_angle=slot.rotation_angle,
            frame_ticks=slot.frame_ticks,
        )

    def release_lease(self, lease: LeasedFrameSlot) -> None:
        if lease._released:
            return
        object.__setattr__(lease, "_released", True)
        lease.slot.readers -= 1
        self._release_retired_slot(lease.slot)
