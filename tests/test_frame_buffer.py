from __future__ import annotations

from dataclasses import FrozenInstanceError
from typing import cast

import pytest

pytest.importorskip("comtypes")

from dxcam.core.stagesurf import StageSurface
from dxcam.runtime.frame_buffer import FrameBuffer, FrameSlot


class FakeStage:
    def __init__(self) -> None:
        self.release_count = 0

    def release(self) -> None:
        self.release_count += 1


def replace_slots(buffer: FrameBuffer) -> list[FakeStage]:
    stages = [FakeStage() for _ in range(3)]
    buffer.replace_slots(
        cast(list[StageSurface], stages),
        frame_width=1920,
        frame_height=1080,
        rotation_angle=0,
    )
    return stages


def publish(buffer: FrameBuffer, ticks: int = 1) -> FrameSlot:
    slot = buffer.reserve_write_slot()
    assert slot is not None
    assert buffer.commit_write(
        slot,
        frame_ticks=ticks,
        frame_width=1920,
        frame_height=1080,
        rotation_angle=0,
    )
    return slot


def test_empty_buffer_has_nothing_to_read_write_or_repeat() -> None:
    buffer = FrameBuffer()
    assert buffer.reserve_write_slot() is None
    assert buffer.lease_latest_slot() is None
    assert not buffer.commit_repeat()
    buffer.clear()
    assert buffer.latest_frame_ticks is None


def test_reservations_exclude_other_writers_and_can_be_cancelled() -> None:
    buffer = FrameBuffer()
    replace_slots(buffer)
    reservations = [buffer.reserve_write_slot() for _ in range(3)]
    assert all(slot is not None for slot in reservations)
    assert len({id(slot) for slot in reservations}) == 3
    assert buffer.reserve_write_slot() is None
    assert buffer.lease_latest_slot() is None
    slot = reservations[0]
    assert slot is not None
    buffer.cancel_write(slot)
    assert buffer.reserve_write_slot() is slot


def test_latest_is_never_reused_when_other_slots_are_leased() -> None:
    buffer = FrameBuffer()
    replace_slots(buffer)
    first = publish(buffer, 1)
    first_lease = buffer.lease_latest_slot()
    second = publish(buffer, 2)
    second_lease = buffer.lease_latest_slot()
    latest = publish(buffer, 3)

    # Previously this fell back to reserving the latest slot, allowing a reader
    # to lease its surface while the producer overwrote it outside the lock.
    assert buffer.reserve_write_slot() is None
    latest_lease = buffer.lease_latest_slot()
    assert latest_lease is not None
    assert latest_lease.slot is latest
    assert not latest.writing
    assert first_lease is not None and second_lease is not None
    buffer.release_lease(first_lease)
    assert buffer.reserve_write_slot() is first
    assert second.readers == 1


def test_reader_and_writer_never_own_the_same_slot() -> None:
    buffer = FrameBuffer()
    replace_slots(buffer)
    current = publish(buffer)
    writer = buffer.reserve_write_slot()
    assert writer is not None and writer is not current
    lease = buffer.lease_latest_slot()
    assert lease is not None and lease.slot is current
    assert current.readers == 1 and not current.writing
    assert writer.readers == 0 and writer.writing


def test_clear_defers_release_until_every_reader_finishes() -> None:
    buffer = FrameBuffer()
    stages = replace_slots(buffer)
    publish(buffer)
    first = buffer.lease_latest_slot()
    second = buffer.lease_latest_slot()
    assert first is not None and second is not None
    buffer.clear()
    buffer.clear()
    assert [stage.release_count for stage in stages] == [0, 1, 1]
    assert buffer.slots == []
    assert not buffer.has_frame
    assert buffer.frame_count == 0
    assert buffer.latest_frame_ticks is None
    assert first.stage is stages[0]
    buffer.release_lease(first)
    buffer.release_lease(first)
    assert stages[0].release_count == 0
    buffer.release_lease(second)
    buffer.release_lease(second)
    assert [stage.release_count for stage in stages] == [1, 1, 1]


def test_old_lease_cannot_release_the_replacement_slot_at_the_same_index() -> None:
    buffer = FrameBuffer()
    old_stages = replace_slots(buffer)
    publish(buffer)
    old_lease = buffer.lease_latest_slot()
    assert old_lease is not None
    new_stages = replace_slots(buffer)
    new_slot = publish(buffer, 2)
    new_lease = buffer.lease_latest_slot()
    assert new_lease is not None
    assert old_stages[0].release_count == 0
    buffer.release_lease(old_lease)
    assert old_stages[0].release_count == 1
    assert new_slot.readers == 1
    assert all(stage.release_count == 0 for stage in new_stages)
    buffer.clear()
    assert new_stages[0].release_count == 0
    buffer.release_lease(new_lease)
    assert all(stage.release_count == 1 for stage in new_stages)


@pytest.mark.parametrize("replace", [False, True])
def test_retiring_writer_defers_release_and_rejects_stale_publication(
    replace: bool,
) -> None:
    buffer = FrameBuffer()
    stages = replace_slots(buffer)
    writer = buffer.reserve_write_slot()
    assert writer is not None
    buffer.clear()
    if replace:
        replace_slots(buffer)
        current = publish(buffer, 2)
    else:
        current = None
    assert stages[0].release_count == 0
    assert not buffer.commit_write(
        writer,
        frame_ticks=1,
        frame_width=1920,
        frame_height=1080,
        rotation_angle=0,
    )
    assert buffer.latest_slot is current
    assert stages[0].release_count == 0
    buffer.cancel_write(writer)
    buffer.cancel_write(writer)
    assert all(stage.release_count == 1 for stage in stages)


def test_failed_capture_releases_reservation_without_changing_latest() -> None:
    buffer = FrameBuffer()
    replace_slots(buffer)
    current = publish(buffer, 1)
    writer = buffer.reserve_write_slot()
    assert writer is not None
    buffer.cancel_write(writer)
    assert not writer.writing
    assert not buffer.commit_write(
        writer,
        frame_ticks=2,
        frame_width=1920,
        frame_height=1080,
        rotation_angle=0,
    )
    assert buffer.latest_slot is current
    assert buffer.frame_count == 1
    assert buffer.latest_frame_ticks == 1


def test_commit_releases_writer_and_finally_cancellation_is_harmless() -> None:
    buffer = FrameBuffer()
    stages = replace_slots(buffer)
    slot = publish(buffer)
    assert not slot.writing
    buffer.cancel_write(slot)
    lease = buffer.lease_latest_slot()
    assert lease is not None and lease.slot is slot
    assert stages[0].release_count == 0


def test_lease_metadata_is_immutable_snapshot_when_slot_is_eventually_reused() -> None:
    buffer = FrameBuffer()
    replace_slots(buffer)
    slot = publish(buffer, 1)
    lease = buffer.lease_latest_slot()
    assert lease is not None
    with pytest.raises(FrozenInstanceError):
        lease.frame_ticks = 99  # type: ignore[misc]
    buffer.release_lease(lease)
    publish(buffer, 2)
    publish(buffer, 3)
    assert buffer.reserve_write_slot() is slot
    assert buffer.commit_write(
        slot,
        frame_ticks=4,
        frame_width=1080,
        frame_height=1920,
        rotation_angle=90,
    )
    assert (
        lease.frame_ticks,
        lease.frame_width,
        lease.frame_height,
        lease.rotation_angle,
    ) == (
        1,
        1920,
        1080,
        0,
    )


def test_repeat_publishes_same_pixels_and_source_timestamp() -> None:
    buffer = FrameBuffer()
    stages = replace_slots(buffer)
    slot = publish(buffer, 42)
    lease = buffer.lease_latest_slot()
    assert lease is not None
    assert buffer.commit_repeat()
    assert buffer.frame_count == 2
    assert buffer.latest_frame_ticks == 42
    assert buffer.latest_slot is slot
    assert slot.readers == 1 and not slot.writing
    assert all(stage.release_count == 0 for stage in stages)


def test_partial_replacement_does_not_retire_current_surfaces() -> None:
    buffer = FrameBuffer()
    stages = replace_slots(buffer)
    slot = publish(buffer)
    with pytest.raises(ValueError, match="requires 3"):
        buffer.replace_slots(
            cast(list[StageSurface], [FakeStage(), FakeStage()]),
            frame_width=1920,
            frame_height=1080,
            rotation_angle=0,
        )
    assert buffer.latest_slot is slot
    assert all(stage.release_count == 0 for stage in stages)
