from __future__ import annotations

from contextlib import contextmanager, nullcontext
from threading import Event
from types import SimpleNamespace

import pytest

import dxcam.dxcam as camera_module
from dxcam.dxcam import DXCamera
from dxcam.runtime.display_recovery import DisplayRecoveryHandler
from dxcam.runtime.output_recovery import OutputState


class FakeOutput:
    resolution = (1920, 1080)
    rotation_angle = 0

    @property
    def surface_size(self):
        if self.rotation_angle in (90, 270):
            return self.resolution[::-1]
        return self.resolution


class FakeStage:
    def __init__(self, *, output, device, dim=None):
        self.output = output
        self.dim = dim or output.surface_size
        self.released = False

    def release(self):
        self.released = True

    def rebind(self, *, output, device):
        self.output = output

    def rebuild(self, dim=None):
        self.released = False
        self.dim = dim or self.output.surface_size

    def ensure_size(self, *, dim):
        self.dim = dim

    def copy_region_from(self, *, src_region, **kwargs):
        assert not self.released
        left, top, right, bottom = src_region
        width, height = self.output.surface_size
        assert 0 <= left < right <= width
        assert 0 <= top < bottom <= height
        assert self.dim == (right - left, bottom - top)


class FakeDuplicator:
    def __init__(self, *, lost=False):
        self.lost = lost
        self.released = False
        self.texture = object()
        self.acquisitions = 0

    def release(self):
        self.released = True

    def reset_frame_tracking(self):
        pass

    @contextmanager
    def acquire_frame(self, **kwargs):
        assert not self.released, "Acquired through a released backend"
        self.acquisitions += 1
        yield not self.lost, not self.lost, self.acquisitions

    @staticmethod
    def ticks_to_seconds(ticks):
        return ticks / 1000


def make_camera(monkeypatch, region=None):
    output = FakeOutput()
    device = SimpleNamespace(context_guard=nullcontext, im_context=object())
    old = FakeDuplicator(lost=True)
    new = FakeDuplicator()
    duplicates = iter((old, new))
    monkeypatch.setattr(camera_module, "StageSurface", FakeStage)
    monkeypatch.setattr(DXCamera, "_create_duplicator", lambda self: next(duplicates))
    camera = DXCamera(output, device, region, output_color="BGRA")
    return camera, output, old, new


def test_stop_interrupts_recovery_and_restart_rebuilds_backend(monkeypatch):
    camera, output, old, new = make_camera(monkeypatch)
    failed = Event()
    available = Event()
    state = OutputState(1280, 720, 0, (0, 0, 1280, 720), False)

    def recover_output(**kwargs):
        if not available.is_set():
            failed.set()
            raise RuntimeError("Display is temporarily unavailable")
        output.resolution = (state.width, state.height)
        return state

    camera._output_recovery.handle = recover_output
    camera._display_recovery._wait.next_delay_seconds = lambda: 60.0
    camera.start(target_fps=0)
    first_worker = camera._DXCamera__worker
    try:
        assert failed.wait(2)
        camera.stop()
        assert not first_worker.is_running()
        assert first_worker.consume_error() is None
        assert camera._recovery_pending
        assert old.released
        assert old.acquisitions == 1

        available.set()
        camera.start(target_fps=0)
        worker = camera._DXCamera__worker
        buffer = camera._DXCamera__frame_buffer
        with worker.frame_condition:
            assert worker.frame_condition.wait_for(lambda: buffer.has_frame, 2)
            lease = buffer.lease_latest_slot()
            assert lease is not None
            assert (lease.frame_width, lease.frame_height) == (1280, 720)
            assert lease.stage.dim == (1280, 720)
            buffer.release_lease(lease)
        assert not camera._recovery_pending
        assert camera.region == state.region
        assert camera._duplicator is new
        assert old.acquisitions == 1
    finally:
        available.set()
        camera.release()


@pytest.mark.parametrize(
    "state",
    [
        OutputState(1280, 720, 0, (0, 0, 1280, 720), False),
        OutputState(2560, 1440, 0, (0, 0, 2560, 1440), False),
        OutputState(720, 1280, 90, (0, 0, 720, 1280), False),
        OutputState(720, 1280, 270, (0, 0, 720, 1280), False),
        OutputState(1280, 720, 0, (10, 20, 1280, 720), True),
    ],
)
def test_recovery_applies_geometry_before_rebuilding_capture_slots(monkeypatch, state):
    camera, output, _, new = make_camera(monkeypatch)

    def recover_output(**kwargs):
        output.resolution = (state.width, state.height)
        output.rotation_angle = state.rotation_angle
        return state

    camera._output_recovery.handle = recover_output
    # Exercise actual allocation and copy geometry without launching a thread.
    camera.is_capturing = True
    try:
        camera._recover_output()
        assert camera._duplicator is new
        assert camera.region == state.region
        buffer = camera._DXCamera__frame_buffer
        assert all(slot.rotation_angle == state.rotation_angle for slot in buffer.slots)
        stage = buffer.slots[0].stage
        captured, _, width, height, rotation = camera._capture_to_stage(
            camera.region, stage
        )
        assert captured
        assert (width, height, rotation) == (
            state.region[2] - state.region[0],
            state.region[3] - state.region[1],
            state.rotation_angle,
        )
    finally:
        camera.release()


def test_already_cancelled_recovery_does_not_release_resources():
    stopped = Event()
    stopped.set()

    def unexpected(*args, **kwargs):
        pytest.fail("Cancelled recovery touched resources")

    handler = DisplayRecoveryHandler(
        backend="dxgi",
        output_recovery=SimpleNamespace(handle=unexpected),
        release_resources=unexpected,
        rebuild_stage_surface=unexpected,
        create_duplicator=unexpected,
        rebuild_frame_buffer=unexpected,
    )
    assert (
        handler.handle(
            region=(0, 0, 10, 10),
            region_set_by_user=False,
            is_capturing=True,
            stop_event=stopped,
        )
        is None
    )


def test_cancellation_during_output_refresh_skips_rebuild():
    stopped = Event()
    releases = []

    def output_handle(**kwargs):
        stopped.set()
        return OutputState(10, 10, 0, (0, 0, 10, 10), False)

    def unexpected(*args, **kwargs):
        pytest.fail("Cancelled recovery rebuilt capture resources")

    handler = DisplayRecoveryHandler(
        backend="dxgi",
        output_recovery=SimpleNamespace(handle=output_handle),
        release_resources=lambda: releases.append(True),
        rebuild_stage_surface=unexpected,
        create_duplicator=unexpected,
        rebuild_frame_buffer=unexpected,
        apply_output_state=unexpected,
    )
    assert (
        handler.handle(
            region=(0, 0, 10, 10),
            region_set_by_user=False,
            is_capturing=True,
            stop_event=stopped,
        )
        is None
    )
    assert releases == [True]


def test_cancellation_after_stage_rebuild_preserves_pending_recovery_on_restart(
    monkeypatch,
):
    camera, output, old, new = make_camera(monkeypatch)
    rebuilt = Event()
    cancelled_slots = []
    state = OutputState(720, 1280, 90, (0, 0, 720, 1280), False)

    def recover_output(**kwargs):
        output.resolution = (state.width, state.height)
        output.rotation_angle = state.rotation_angle
        return state

    original_rebuild = camera._display_recovery._rebuild_stage_surface

    def rebuild_then_cancel_once():
        original_rebuild()
        if not rebuilt.is_set():
            cancelled_slots.extend(camera._DXCamera__frame_buffer.slots)
            camera._DXCamera__worker.stop()
            rebuilt.set()

    camera._output_recovery.handle = recover_output
    camera._display_recovery._rebuild_stage_surface = rebuild_then_cancel_once
    camera.start(target_fps=0)
    first_worker = camera._DXCamera__worker
    try:
        assert rebuilt.wait(2)
        camera.stop()
        assert not first_worker.is_running()
        assert first_worker.consume_error() is None
        assert camera._recovery_pending
        assert camera._duplicator is old
        assert old.released
        assert old.acquisitions == 1
        assert new.acquisitions == 0
        assert camera.rotation_angle == 90
        assert camera.region == state.region
        assert len(cancelled_slots) == 3
        assert all(slot.stage.released for slot in cancelled_slots)

        camera.start(target_fps=0)
        worker = camera._DXCamera__worker
        buffer = camera._DXCamera__frame_buffer
        with worker.frame_condition:
            assert worker.frame_condition.wait_for(lambda: buffer.has_frame, 2)
            lease = buffer.lease_latest_slot()
            assert lease is not None
            assert (lease.frame_width, lease.frame_height, lease.rotation_angle) == (
                720,
                1280,
                90,
            )
            assert lease.stage.dim == (1280, 720)
            buffer.release_lease(lease)
        assert not camera._recovery_pending
        assert camera._duplicator is new
        assert old.acquisitions == 1
        assert new.acquisitions > 0
    finally:
        camera.release()
