from __future__ import annotations

from threading import Event, Lock, Thread, current_thread
from types import SimpleNamespace

import numpy as np
import pytest

from dxcam.core.stagesurf import StageSurface
from dxcam.dxcam import DXCamera
from dxcam.processor import Processor
from dxcam.runtime.capture_worker import CaptureWorker
from dxcam.runtime.frame_buffer import FrameBuffer


class FakeStage:
    def __init__(self):
        self.releases = 0

    def release(self):
        self.releases += 1


class FakeWorker:
    thread = None
    elapsed_seconds = 0

    def __init__(self, error=None, join_ok=True):
        self.running = True
        self.error = error
        self.join_ok = join_ok

    def stop(self):
        pass

    def join(self, timeout=None):
        self.running = not self.join_ok
        return self.join_ok

    def consume_error(self):
        return self.error

    def is_running(self):
        return self.running

    def clear_frame_signal(self):
        pass

    def wait_for_frame(self, timeout):
        raise AssertionError("A published frame must not wait for another event")


def make_buffer():
    buffer = FrameBuffer()
    stages = [FakeStage() for _ in range(3)]
    buffer.replace_slots(stages, frame_width=2, frame_height=2, rotation_angle=0)
    return buffer, stages


def publish(buffer, rotation_angle=0):
    slot = buffer.reserve_write_slot()
    assert slot is not None
    assert buffer.commit_write(
        slot,
        frame_ticks=123,
        frame_width=2,
        frame_height=2,
        rotation_angle=rotation_angle,
    )
    return slot


def make_camera():
    camera = DXCamera.__new__(DXCamera)
    camera._is_released = False
    camera.is_capturing = True
    camera._DXCamera__lock = Lock()
    camera._DXCamera__processor_lock = Lock()
    camera._DXCamera__frame_buffer, stages = make_buffer()
    camera._DXCamera__worker = FakeWorker()
    camera._DXCamera__last_grab_entry = None
    camera._duplicator = SimpleNamespace(
        ticks_to_seconds=lambda value: value / 100, release=lambda: None
    )
    camera._stagesurf = FakeStage()
    camera.width = camera.height = 2
    camera.region = (0, 0, 2, 2)
    camera.channel_size = 3
    camera._process_stage = lambda **kwargs: np.zeros((2, 2, 3), dtype=np.uint8)
    return camera, stages


def test_reading_published_frame_does_not_require_another_capture_event():
    camera, _ = make_camera()
    publish(camera._DXCamera__frame_buffer)
    try:
        for _ in range(2):
            frame, timestamp = camera.get_latest_frame(with_timestamp=True)
            assert frame.shape == (2, 2, 3)
            assert timestamp == 1.23
    finally:
        camera.release()


def test_stop_retires_surface_until_inflight_read_finishes():
    camera, stages = make_camera()
    publish(camera._DXCamera__frame_buffer)
    reading, finish = Event(), Event()
    errors = []

    def process(**kwargs):
        reading.set()
        assert finish.wait(5)
        assert kwargs["stage"].releases == 0
        return np.zeros((2, 2, 3), dtype=np.uint8)

    camera._process_stage = process

    def read():
        try:
            assert camera.get_latest_frame().shape == (2, 2, 3)
        except BaseException as exc:
            errors.append(exc)

    thread = Thread(target=read)
    thread.start()
    try:
        assert reading.wait(5)
        camera.stop()
        assert [stage.releases for stage in stages] == [0, 1, 1]
    finally:
        finish.set()
        thread.join(5)
        camera.release()
    assert not thread.is_alive()
    assert not errors
    assert [stage.releases for stage in stages] == [1, 1, 1]


def test_waiter_does_not_consume_a_restarted_sessions_frame():
    camera, _ = make_camera()
    old_worker = camera._DXCamera__worker

    def restart(timeout):
        camera._DXCamera__worker = FakeWorker()
        publish(camera._DXCamera__frame_buffer)
        return True

    old_worker.wait_for_frame = restart
    try:
        assert camera.get_latest_frame() is None
        assert camera.get_latest_frame() is not None
    finally:
        camera.release()


def test_capture_exception_cancels_writer_reservation():
    buffer, _ = make_buffer()

    def fail(region, stage):
        raise RuntimeError("capture failed")

    worker = CaptureWorker(buffer, Lock(), fail, lambda: (0, 0, 2, 2), target_fps=0)
    worker.run_loop()
    assert str(worker.consume_error()) == "capture failed"
    assert worker.wait_for_frame(timeout=0)
    assert not any(slot.writing for slot in buffer.slots)
    buffer.clear()


def test_recovery_during_capture_cannot_publish_retired_writer():
    buffer, stages = make_buffer()
    replacements = [FakeStage() for _ in range(3)]

    def recover(region, stage):
        buffer.replace_slots(
            replacements, frame_width=2, frame_height=2, rotation_angle=0
        )
        assert stage.releases == 0
        return True, 456, 2, 2, 0

    worker = CaptureWorker(buffer, Lock(), recover, lambda: (0, 0, 2, 2))
    worker._run_capture_cycle()
    assert not buffer.has_frame
    assert [stage.releases for stage in stages] == [1, 1, 1]
    assert all(stage.releases == 0 for stage in replacements)
    buffer.clear()


def test_timer_setup_error_wakes_waiters_and_is_reported(monkeypatch):
    buffer, _ = make_buffer()
    closed = []
    monkeypatch.setattr(
        "dxcam.runtime.capture_worker.create_high_resolution_timer", lambda: "timer"
    )

    def fail(timer, fps):
        raise OSError("timer failed")

    monkeypatch.setattr("dxcam.runtime.capture_worker.set_periodic_timer", fail)
    monkeypatch.setattr("dxcam.runtime.capture_worker.cancel_timer", closed.append)
    worker = CaptureWorker(buffer, Lock(), lambda *args: None, lambda: (0, 0, 2, 2))
    worker.run_loop()
    assert str(worker.consume_error()) == "timer failed"
    assert worker.wait_for_frame(timeout=0)
    assert closed == ["timer"]
    buffer.clear()


def test_release_cleans_resources_after_reporting_capture_error():
    camera, stages = make_camera()
    camera._DXCamera__worker = FakeWorker(error=RuntimeError("producer failed"))
    released = []
    camera._duplicator.release = lambda: released.append(True)
    with pytest.raises(RuntimeError, match="producer failed"):
        camera.release()
    assert camera.is_released
    assert released == [True]
    assert camera._stagesurf.releases == 1
    assert [stage.releases for stage in stages] == [1, 1, 1]
    camera.release()
    assert released == [True]


def test_release_join_timeout_keeps_live_capture_resources():
    camera, stages = make_camera()
    worker = camera._DXCamera__worker = FakeWorker(join_ok=False)
    try:
        with pytest.raises(RuntimeError, match="did not stop"):
            camera.release()
        assert not camera.is_released
        assert all(stage.releases == 0 for stage in stages)
        assert camera._stagesurf.releases == 0
    finally:
        worker.join_ok = True
        camera.release()


def test_allocation_failure_leaves_camera_stopped():
    camera, _ = make_camera()
    camera.stop()

    def fail(*args, **kwargs):
        raise MemoryError("stage allocation")

    camera._allocate_capture_slots_for_region = fail
    try:
        with pytest.raises(MemoryError, match="stage allocation"):
            camera.start()
        assert not camera.is_capturing
        assert camera._DXCamera__worker is None
    finally:
        camera.release()


def test_mapped_surface_holds_lock_through_unmap_even_on_error():
    stage = StageSurface.__new__(StageSurface)
    stage._map_lock = Lock()
    events = []

    def mapped():
        assert stage._map_lock.locked()
        events.append("map")
        return "pixels"

    def unmapped():
        assert stage._map_lock.locked()
        events.append("unmap")

    stage.map = mapped
    stage.unmap = unmapped
    with pytest.raises(ValueError, match="processor failed"):
        with stage.mapped() as rect:
            assert rect == "pixels"
            assert not stage._map_lock.acquire(blocking=False)
            raise ValueError("processor failed")
    assert events == ["map", "unmap"]
    assert stage._map_lock.acquire(blocking=False)
    stage._map_lock.release()


def test_readers_of_different_slots_do_not_overwrite_rotated_processor_scratch():
    pytest.importorskip("cv2")
    from dxcam.processor.cv2_processor import _NUMPY_KERNELS_AVAILABLE

    if not _NUMPY_KERNELS_AVAILABLE:
        pytest.skip("Compiled rotation preparation is not available")

    first_prepared, second_at_boundary, finish_first = Event(), Event(), Event()

    class NotifyingLock:
        """Let the test advance once the second reader is waiting or finished."""

        def __init__(self):
            self.lock = Lock()

        def __enter__(self):
            if not self.lock.acquire(blocking=False):
                second_at_boundary.set()
                self.lock.acquire()

        def __exit__(self, *args):
            self.lock.release()

    class MappedStage(FakeStage):
        mapped = StageSurface.mapped

        def __init__(self, value):
            super().__init__()
            self.pixels = np.full((2, 2, 4), value, dtype=np.uint8)
            self._map_lock = Lock()

        def map(self):
            assert self.releases == 0
            return SimpleNamespace(Pitch=8, pBits=self.pixels.ctypes.data)

        def unmap(self):
            assert self.releases == 0

    camera, _ = make_camera()
    del camera._process_stage
    camera._DXCamera__processor_lock = NotifyingLock()
    camera._processor = Processor(backend="cv2", output_color="RGB")
    processor = camera._processor._impl
    processor._ensure_cvtcolor_initialized()

    def hold_prepared_frame():
        # process_into() calls this after preparing the shared rotation scratch,
        # immediately before cvtColor reads it.
        if current_thread().name == "first-reader":
            first_prepared.set()
            assert finish_first.wait(5)

    processor._ensure_cvtcolor_initialized = hold_prepared_frame
    stages = [MappedStage(value) for value in (11, 22, 33)]
    buffer = camera._DXCamera__frame_buffer
    buffer.replace_slots(stages, frame_width=2, frame_height=2, rotation_angle=180)
    publish(buffer, rotation_angle=180)
    frames, errors = {}, []

    def read(index):
        try:
            frames[index] = camera.get_latest_frame()
        except BaseException as exc:
            errors.append(exc)
        finally:
            if index == 1:
                second_at_boundary.set()

    first = Thread(target=read, args=(0,), name="first-reader")
    second = Thread(target=read, args=(1,), name="second-reader")
    first.start()
    try:
        assert first_prepared.wait(5)
        # A new publication gives the second reader a different surface lock.
        with camera._DXCamera__lock:
            publish(buffer, rotation_angle=180)
        second.start()
        assert second_at_boundary.wait(5)
        # Both readers already own their slots; stop must retire them safely even
        # while the second reader is waiting for the shared processor.
        camera.stop()
        assert stages[0].releases == 0
    finally:
        finish_first.set()
        first.join(5)
        if second.ident is not None:
            second.join(5)
        camera.release()
    assert not first.is_alive() and not second.is_alive()
    assert not errors
    np.testing.assert_array_equal(frames[0], np.full((2, 2, 3), 11, dtype=np.uint8))
    np.testing.assert_array_equal(frames[1], np.full((2, 2, 3), 22, dtype=np.uint8))
    assert [stage.releases for stage in stages] == [1, 1, 1]
