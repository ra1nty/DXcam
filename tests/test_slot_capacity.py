from __future__ import annotations

from queue import Queue
from threading import Event, Lock, Thread
from types import SimpleNamespace

import pytest

from dxcam.dxcam import DXCamera
from dxcam.runtime.capture_worker import CaptureWorker
from dxcam.runtime.frame_buffer import FrameBuffer


class FakeStage:
    def __init__(self, **kwargs):
        self.releases = 0

    def release(self):
        self.releases += 1


def replace_slots(buffer):
    stages = [FakeStage() for _ in range(3)]
    buffer.replace_slots(stages, frame_width=2, frame_height=2, rotation_angle=0)
    return stages


def publish(buffer, ticks):
    slot = buffer.reserve_write_slot()
    assert slot is not None
    assert buffer.commit_write(
        slot,
        frame_ticks=ticks,
        frame_width=2,
        frame_height=2,
        rotation_angle=0,
    )
    return slot


class CapacityHarness:
    """Real worker/camera lease synchronization without native capture."""

    def __init__(self):
        self.lock = Lock()
        self.buffer = FrameBuffer()
        self.stages = replace_slots(self.buffer)
        self.contexts = []
        self.workers = []
        self.captures = []
        self.captured = Event()
        camera = self.camera = DXCamera.__new__(DXCamera)
        camera._is_released = False
        camera.backend = "dxgi"
        camera.is_capturing = True
        camera._DXCamera__lock = self.lock
        camera._DXCamera__frame_buffer = self.buffer
        camera._DXCamera__last_grab_entry = None
        camera._duplicator = SimpleNamespace(
            ticks_to_seconds=lambda ticks: float(ticks), release=lambda: None
        )
        camera._stagesurf = FakeStage()
        camera._output = SimpleNamespace(surface_size=(2, 2))
        camera._device = object()
        camera.width = camera.height = 2
        camera.rotation_angle = 0
        camera.channel_size = 3
        camera.region = (0, 0, 2, 2)
        self.install_worker()

    def install_worker(self, *, target_fps=0, video_mode=False):
        worker = CaptureWorker(
            self.buffer,
            self.lock,
            self.capture,
            lambda: self.camera.region,
            target_fps=target_fps,
            video_mode=video_mode,
        )
        self.workers.append(worker)
        self.worker = self.camera._DXCamera__worker = worker
        self.camera.is_capturing = True
        self.waits = Queue()
        original_wait = worker.capacity_condition.wait

        def observe_wait(timeout=None):
            # Called while holding the frame lock. A test acquiring that lock
            # after receiving this signal knows the producer entered its wait.
            self.waits.put(True)
            return original_wait(timeout)

        worker.capacity_condition.wait = observe_wait
        return worker

    def capture(self, region, stage):
        self.captures.append(stage)
        self.captured.set()
        self.worker.stop()
        return False, 0, 2, 2, 0

    def publish(self, ticks):
        with self.lock:
            return publish(self.buffer, ticks)

    def lease(self):
        context = self.camera._read_lease()
        lease = context.__enter__()
        assert lease is not None
        self.contexts.append(context)
        return context, lease

    def release(self, context):
        self.contexts.remove(context)
        context.__exit__(None, None, None)

    def saturate(self):
        self.publish(1)
        first = self.lease()
        self.publish(2)
        second = self.lease()
        self.publish(3)
        return first, second

    def wait_until_blocked(self):
        assert self.waits.get(timeout=2)
        with self.lock:
            assert not self.captures
            assert not any(slot.writing for slot in self.buffer.slots)

    def join(self):
        assert self.worker.join(timeout=2), "producer did not finish"
        assert self.worker.consume_error() is None

    def close(self):
        for worker in self.workers:
            worker.stop()
        try:
            for worker in self.workers:
                assert worker.join(timeout=2), "producer survived cleanup"
        finally:
            for context in list(self.contexts):
                self.release(context)
            with self.lock:
                self.buffer.clear()
            self.camera._is_released = True


@pytest.fixture
def harness():
    value = CapacityHarness()
    try:
        yield value
    finally:
        value.close()


def test_unpaced_producer_waits_for_camera_lease_release(harness):
    (first_context, first), _ = harness.saturate()
    harness.worker.start()
    harness.wait_until_blocked()

    # This uses the actual camera finally block, not a manual notification.
    harness.release(first_context)
    assert harness.captured.wait(2)
    harness.join()
    assert harness.captures == [first.stage]
    assert not any(slot.writing for slot in harness.buffer.slots)


@pytest.mark.parametrize("video_mode", [False, True])
def test_spurious_wake_rechecks_capacity_without_repeating_again(harness, video_mode):
    harness.worker.video_mode = video_mode
    harness.saturate()
    before = harness.buffer.frame_count
    harness.worker.start()
    harness.wait_until_blocked()
    with harness.lock:
        assert harness.buffer.frame_count == before + int(video_mode)
        assert harness.buffer.latest_frame_ticks == 3
        harness.worker.capacity_condition.notify_all()

    harness.wait_until_blocked()
    with harness.lock:
        assert harness.buffer.frame_count == before + int(video_mode)
        assert harness.buffer.latest_frame_ticks == 3
    assert harness.camera._wait_for_read_lease(after_timestamp=3, timeout=0) is None
    harness.worker.stop()
    harness.join()
    assert not harness.captures


def test_stop_wakes_capacity_waiter_and_fresh_reader(harness):
    harness.saturate()
    harness.worker.start()
    harness.wait_until_blocked()
    reader_waiting = Event()
    results = Queue()
    original_wait = harness.worker.frame_condition.wait

    def observe_reader_wait(timeout=None):
        reader_waiting.set()
        return original_wait(timeout)

    harness.worker.frame_condition.wait = observe_reader_wait

    def read():
        results.put(harness.camera._wait_for_read_lease(after_timestamp=3))

    reader = Thread(target=read, daemon=True)
    reader.start()
    try:
        assert reader_waiting.wait(2)
        harness.worker.stop()
        harness.join()
        assert results.get(timeout=2) is None
        assert not harness.captures
        assert not any(slot.writing for slot in harness.buffer.slots)
    finally:
        harness.worker.stop()
        reader.join(2)
        assert not reader.is_alive()


@pytest.mark.parametrize("video_mode", [False, True])
def test_paced_exhaustion_returns_to_timer_and_preserves_repeat_semantics(
    harness, video_mode
):
    harness.worker.target_fps = 120
    harness.worker.video_mode = video_mode
    harness.saturate()
    before = harness.buffer.frame_count

    def must_not_wait(timeout=None):
        pytest.fail("paced producer must return to its timer")

    harness.worker.capacity_condition.wait = must_not_wait
    for _ in range(3):
        harness.worker._run_capture_cycle()
    assert harness.buffer.frame_count == before + 3 * int(video_mode)
    assert harness.buffer.latest_frame_ticks == 3
    assert harness.camera._wait_for_read_lease(after_timestamp=3, timeout=0) is None
    assert not harness.captures


def test_only_final_reader_of_reusable_slot_releases_capacity():
    buffer = FrameBuffer()
    replace_slots(buffer)
    first = publish(buffer, 1)
    lease_a = buffer.lease_latest_slot()
    lease_b = buffer.lease_latest_slot()
    assert lease_a is not None and lease_b is not None
    publish(buffer, 2)
    assert buffer.release_lease(lease_a) is False
    assert first.readers == 1
    assert buffer.release_lease(lease_a) is False
    assert first.readers == 1
    assert buffer.release_lease(lease_b) is True
    assert first.readers == 0
    assert buffer.release_lease(lease_b) is False
    buffer.clear()


def test_latest_slot_release_does_not_create_write_capacity():
    buffer = FrameBuffer()
    replace_slots(buffer)
    current = publish(buffer, 1)
    lease = buffer.lease_latest_slot()
    assert lease is not None
    assert buffer.release_lease(lease) is False
    assert current.readers == 0
    assert buffer.latest_slot is current
    buffer.clear()


def test_retired_lease_release_does_not_create_current_generation_capacity():
    buffer = FrameBuffer()
    old_stages = replace_slots(buffer)
    publish(buffer, 1)
    old = buffer.lease_latest_slot()
    assert old is not None
    replacements = replace_slots(buffer)
    assert buffer.release_lease(old) is False
    assert old_stages[0].releases == 1
    assert all(stage.releases == 0 for stage in replacements)
    assert buffer.release_lease(old) is False
    assert old_stages[0].releases == 1
    buffer.clear()


def test_partial_and_latest_camera_releases_do_not_notify_capacity(harness):
    harness.publish(1)
    first_context, _ = harness.lease()
    shared_context, _ = harness.lease()
    harness.publish(2)
    latest_context, _ = harness.lease()
    notifications = []
    original_notify = harness.worker.capacity_condition.notify

    def observe_notify(n=1):
        notifications.append(n)
        return original_notify(n)

    harness.worker.capacity_condition.notify = observe_notify
    harness.release(first_context)
    harness.release(latest_context)
    assert notifications == []
    harness.release(shared_context)
    assert len(notifications) == 1


def test_replacement_wakes_producer_and_reserves_only_new_generation(harness):
    harness.saturate()
    harness.worker.start()
    harness.wait_until_blocked()
    with harness.lock:
        replacements = replace_slots(harness.buffer)
        harness.worker.capacity_condition.notify_all()
    assert harness.captured.wait(2)
    harness.join()
    assert len(harness.captures) == 1
    assert harness.captures[0] in replacements
    assert all(stage.releases == 0 for stage in replacements)


def test_camera_allocation_notifies_existing_worker_after_replacing_slots(
    harness, monkeypatch
):
    monkeypatch.setattr("dxcam.dxcam.StageSurface", FakeStage)
    notifications = []
    old_slots = list(harness.buffer.slots)
    original_notify = harness.worker.capacity_condition.notify

    def observe_notify(n=1):
        assert len(harness.buffer.slots) == 3
        assert all(slot not in old_slots for slot in harness.buffer.slots)
        notifications.append(n)
        return original_notify(n)

    harness.worker.capacity_condition.notify = observe_notify
    with harness.lock:
        harness.camera._allocate_capture_slots_for_region(
            harness.camera.region, reason="test-replacement"
        )
    assert notifications


def test_delayed_old_session_release_does_not_wake_restarted_producer(harness):
    (old_context, old_lease), _ = harness.saturate()
    old_worker = harness.worker
    old_worker.start()
    harness.wait_until_blocked()
    harness.camera.stop()
    assert old_worker.stopped
    assert old_lease.slot.retired

    with harness.lock:
        replace_slots(harness.buffer)
    new_worker = harness.install_worker()
    (new_context, new_lease), _ = harness.saturate()
    new_worker.start()
    harness.wait_until_blocked()
    notifications = []
    original_notify = new_worker.capacity_condition.notify

    def observe_notify(n=1):
        notifications.append(n)
        return original_notify(n)

    new_worker.capacity_condition.notify = observe_notify
    harness.release(old_context)
    assert old_lease.stage.releases == 1
    assert notifications == []
    assert not harness.captures
    harness.release(new_context)
    assert harness.captured.wait(2)
    harness.join()
    assert harness.captures == [new_lease.stage]
    assert old_worker.stopped


def test_release_before_wait_handshake_is_not_lost(harness):
    (first_context, first), _ = harness.saturate()
    entering_wait = Event()
    release_requested = Event()
    original_wait = harness.worker.capacity_condition.wait

    def rendezvous_wait(timeout=None):
        entering_wait.set()
        assert release_requested.wait(2)
        # The reader is already trying to acquire the frame lock. Condition.wait
        # must register this waiter before atomically releasing that lock.
        return original_wait(timeout)

    harness.worker.capacity_condition.wait = rendezvous_wait

    def release():
        assert entering_wait.wait(2)
        release_requested.set()
        harness.release(first_context)

    releaser = Thread(target=release, daemon=True)
    releaser.start()
    harness.worker.start()
    try:
        assert harness.captured.wait(2)
        harness.join()
        assert harness.captures == [first.stage]
    finally:
        release_requested.set()
        harness.worker.stop()
        releaser.join(2)
        assert not releaser.is_alive()
