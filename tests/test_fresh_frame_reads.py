from __future__ import annotations

from queue import Empty, Queue
from threading import Event, Lock, Thread, current_thread
from time import monotonic
from types import SimpleNamespace

import numpy as np
import pytest

from dxcam.dxcam import DXCamera
from dxcam.runtime.capture_worker import CaptureWorker
from dxcam.runtime.frame_buffer import FrameBuffer


class FakeStage:
    def __init__(self):
        self.pixel = 0

    def release(self):
        pass


class PendingRead:
    def __init__(self, callback, name):
        self.waiting = Event()
        self.done = Event()
        self.result = None
        self.error = None

        def run():
            try:
                self.result = callback()
            except BaseException as exc:
                self.error = exc
            finally:
                self.done.set()

        self.thread = Thread(target=run, name=name, daemon=True)

    def finish(self):
        assert self.done.wait(2), "reader did not finish"
        self.thread.join(2)
        assert self.error is None, repr(self.error)
        return self.result


class CameraHarness:
    """Real camera/worker synchronization with queue-fed synthetic frames."""

    def __init__(self):
        self.requests = Queue()
        self.readers = {}
        self.capture_options = []
        self.buffer = FrameBuffer()
        camera = self.camera = DXCamera.__new__(DXCamera)
        camera._is_released = False
        camera.is_capturing = False
        camera._DXCamera__lock = Lock()
        camera._DXCamera__processor_lock = Lock()
        camera._DXCamera__frame_buffer = self.buffer
        camera._DXCamera__worker = None
        camera._DXCamera__last_grab_entry = None
        camera._duplicator = SimpleNamespace(
            ticks_to_seconds=lambda ticks: ticks / 100,
            release=lambda: None,
            reset_frame_tracking=lambda: None,
        )
        camera._stagesurf = FakeStage()
        camera.width = camera.height = 2
        camera.rotation_angle = 0
        camera.channel_size = 3
        camera.region = (0, 0, 2, 2)
        camera._allocate_capture_slots_for_region = self.allocate
        camera._capture_to_stage = self.capture
        camera._process_stage = self.process

    @property
    def worker(self):
        return self.camera._DXCamera__worker

    def allocate(self, region, *, reason):
        self.buffer.replace_slots(
            [FakeStage() for _ in range(3)],
            frame_width=2,
            frame_height=2,
            rotation_angle=0,
        )

    def capture(self, region, stage, *, wait_for_frame=True, timeout_ms=None):
        self.capture_options.append((wait_for_frame, timeout_ms))
        worker = self.worker
        while not worker.stopped:
            try:
                request = self.requests.get(timeout=0.01)
            except Empty:
                continue
            if isinstance(request, Exception):
                raise request
            if request is None:
                return False, 0, 2, 2, 0
            ticks, stage.pixel = request
            return True, ticks, 2, 2, 0
        return False, 0, 2, 2, 0

    @staticmethod
    def process(*, stage, frame_width, frame_height, rotation_angle, dst=None):
        if dst is None:
            dst = np.empty((frame_height, frame_width, 3), dtype=np.uint8)
        dst.fill(stage.pixel)
        return dst

    def start(self, **kwargs):
        self.camera.start(target_fps=0, **kwargs)
        condition = self.worker.frame_condition
        original_wait = condition.wait

        def observe_wait(timeout=None):
            reader = self.readers.get(current_thread().name)
            if reader is not None:
                reader.waiting.set()
            return original_wait(timeout)

        condition.wait = observe_wait

    def publish(self, ticks, pixel=1):
        with self.worker.frame_condition:
            previous_count = self.buffer.frame_count
            self.requests.put((ticks, pixel))
            assert self.worker.frame_condition.wait_for(
                lambda: self.buffer.frame_count > previous_count, timeout=2
            ), "worker did not publish"
            assert self.buffer.latest_frame_ticks == ticks

    def repeat(self):
        with self.worker.frame_condition:
            previous_count = self.buffer.frame_count
            self.requests.put(None)
            assert self.worker.frame_condition.wait_for(
                lambda: self.buffer.frame_count > previous_count, timeout=2
            ), "worker did not repeat"

    def read_async(self, callback):
        name = f"fresh-reader-{len(self.readers)}"
        reader = PendingRead(callback, name)
        self.readers[name] = reader
        reader.thread.start()
        return reader

    def close(self):
        self.camera.release()
        for reader in self.readers.values():
            reader.thread.join(2)
            assert not reader.thread.is_alive(), "reader survived camera release"


@pytest.fixture
def harness():
    harness = CameraHarness()
    try:
        yield harness
    finally:
        harness.close()


def test_default_reads_reuse_latest_and_fresh_reads_skip_missed_frames(harness):
    harness.start()
    harness.publish(100, pixel=11)
    camera = harness.camera
    first, timestamp = camera.get_latest_frame(with_timestamp=True)
    assert timestamp == 1.0
    harness.publish(200, pixel=22)
    harness.publish(300, pixel=33)
    latest, timestamp = camera.get_latest_frame(
        with_timestamp=True, after_timestamp=timestamp, timeout=0
    )
    assert timestamp == 3.0
    assert np.all(first == 11)
    assert np.all(latest == 33)
    for _ in range(2):
        repeated, repeated_timestamp = camera.get_latest_frame(with_timestamp=True)
        assert repeated_timestamp == 3.0
        assert np.array_equal(repeated, latest)
        assert not np.shares_memory(repeated, latest)
    assert camera.get_latest_frame(after_timestamp=timestamp, timeout=0) is None


def test_readers_have_independent_thresholds_and_all_wake(harness):
    harness.start()
    harness.publish(100)
    camera = harness.camera
    readers = [
        harness.read_async(
            lambda threshold=threshold: camera.get_latest_frame(
                with_timestamp=True, after_timestamp=threshold
            )
        )
        for threshold in (1.0, 1.0, 2.0)
    ]
    for reader in readers:
        assert reader.waiting.wait(2)
    harness.publish(200)
    assert readers[0].finish()[1] == 2.0
    assert readers[1].finish()[1] == 2.0
    assert not readers[2].done.is_set()
    harness.publish(300)
    assert readers[2].finish()[1] == 3.0


@pytest.mark.parametrize("into", [False, True])
def test_first_frame_wait_is_woken_by_publication(harness, into):
    harness.start()
    dst = np.full((2, 2, 3), 99, dtype=np.uint8)
    read = (
        (lambda: harness.camera.get_latest_frame_into(dst, with_timestamp=True))
        if into
        else lambda: harness.camera.get_latest_frame(with_timestamp=True)
    )
    reader = harness.read_async(read)
    assert reader.waiting.wait(2)
    harness.publish(125, pixel=17)
    result, timestamp = reader.finish()
    assert timestamp == 1.25
    if into:
        assert result is True
        assert np.all(dst == 17)
    else:
        assert np.all(result == 17)


@pytest.mark.parametrize("into", [False, True])
@pytest.mark.parametrize("published", [False, True])
@pytest.mark.parametrize("timeout", [0, 0.02])
def test_unavailable_reads_respect_timeout_and_preserve_destination(
    harness, into, published, timeout
):
    harness.start()
    if published:
        harness.publish(100)
    dst = np.full((2, 2, 3), 99, dtype=np.uint8)
    before = monotonic()
    kwargs = {"after_timestamp": 1.0, "timeout": timeout}
    if into:
        result = harness.camera.get_latest_frame_into(dst, **kwargs)
    else:
        result = harness.camera.get_latest_frame(**kwargs)
    elapsed = monotonic() - before
    assert result is None
    assert elapsed < 2
    if timeout:
        assert elapsed >= timeout * 0.75
    assert np.all(dst == 99)


def test_video_repeats_and_equal_timestamps_do_not_satisfy_fresh_read(harness):
    harness.start(video_mode=True)
    harness.publish(100)
    reader = harness.read_async(
        lambda: harness.camera.get_latest_frame(
            with_timestamp=True, after_timestamp=1.0
        )
    )
    assert reader.waiting.wait(2)
    harness.repeat()
    harness.publish(100, pixel=2)
    assert harness.camera.latest_frame_time == 1.0
    assert harness.camera.get_latest_frame(after_timestamp=1.0, timeout=0) is None
    assert not reader.done.is_set()
    harness.publish(101, pixel=3)
    frame, timestamp = reader.finish()
    assert timestamp == 1.01
    assert np.all(frame == 3)


@pytest.mark.parametrize("into", [False, True])
def test_stop_wakes_waiter_without_switching_to_restarted_session(harness, into):
    harness.start()
    harness.publish(100)
    dst = np.full((2, 2, 3), 99, dtype=np.uint8)
    read = (
        (lambda: harness.camera.get_latest_frame_into(dst, after_timestamp=1.0))
        if into
        else lambda: harness.camera.get_latest_frame(after_timestamp=1.0)
    )
    reader = harness.read_async(read)
    assert reader.waiting.wait(2)
    harness.camera.stop()
    harness.start()
    harness.publish(200, pixel=2)
    assert reader.finish() is None
    assert np.all(dst == 99)
    assert harness.camera.get_latest_frame(with_timestamp=True, timeout=0)[1] == 2.0


@pytest.mark.parametrize("into", [False, True])
def test_worker_failure_wakes_fresh_reader_and_is_reported_by_stop(harness, into):
    harness.start()
    harness.publish(100)
    dst = np.full((2, 2, 3), 99, dtype=np.uint8)
    read = (
        (lambda: harness.camera.get_latest_frame_into(dst, after_timestamp=1.0))
        if into
        else lambda: harness.camera.get_latest_frame(after_timestamp=1.0)
    )
    reader = harness.read_async(read)
    assert reader.waiting.wait(2)
    harness.requests.put(RuntimeError("synthetic capture failure"))
    assert reader.finish() is None
    assert np.all(dst == 99)
    with pytest.raises(RuntimeError, match="synthetic capture failure"):
        harness.camera.stop()
    assert harness.camera.get_latest_frame(timeout=0) is None


@pytest.mark.parametrize("into", [False, True])
def test_timestamp_matches_leased_pixels_and_timeout_does_not_limit_conversion(
    harness, into
):
    harness.start()
    harness.publish(100, pixel=11)
    processing, finish_processing = Event(), Event()
    original_process = harness.camera._process_stage

    def process(**kwargs):
        processing.set()
        assert finish_processing.wait(2)
        return original_process(**kwargs)

    harness.camera._process_stage = process
    dst = np.full((2, 2, 3), 99, dtype=np.uint8)
    kwargs = {"with_timestamp": True, "after_timestamp": 0.0, "timeout": 0}
    read = (
        (lambda: harness.camera.get_latest_frame_into(dst, **kwargs))
        if into
        else lambda: harness.camera.get_latest_frame(**kwargs)
    )
    reader = harness.read_async(read)
    try:
        assert processing.wait(2)
        harness.publish(200, pixel=22)
    finally:
        finish_processing.set()
    result, timestamp = reader.finish()
    assert timestamp == 1.0
    if into:
        assert result is True
        assert np.all(dst == 11)
    else:
        assert np.all(result == 11)
    latest, latest_timestamp = harness.camera.get_latest_frame(
        with_timestamp=True, timeout=0
    )
    assert latest_timestamp == 2.0
    assert np.all(latest == 22)


@pytest.mark.parametrize("into", [False, True])
@pytest.mark.parametrize(
    "argument,value,error",
    [
        ("timeout", -0.1, ValueError),
        ("timeout", float("nan"), ValueError),
        ("timeout", float("inf"), ValueError),
        ("timeout", float("-inf"), ValueError),
        ("timeout", True, TypeError),
        ("timeout", "1", TypeError),
        ("after_timestamp", float("nan"), ValueError),
        ("after_timestamp", float("inf"), ValueError),
        ("after_timestamp", float("-inf"), ValueError),
        ("after_timestamp", False, TypeError),
        ("after_timestamp", "1", TypeError),
    ],
)
def test_invalid_read_options_are_rejected_even_when_stopped(
    harness, into, argument, value, error
):
    kwargs = {argument: value}
    dst = np.full((2, 2, 3), 99, dtype=np.uint8)
    with pytest.raises(error):
        if into:
            harness.camera.get_latest_frame_into(dst, **kwargs)
        else:
            harness.camera.get_latest_frame(**kwargs)
    assert np.all(dst == 99)


def test_finite_negative_threshold_is_valid(harness):
    harness.start()
    harness.publish(0, pixel=7)
    frame, timestamp = harness.camera.get_latest_frame(
        with_timestamp=True, after_timestamp=-1, timeout=0
    )
    assert timestamp == 0.0
    assert np.all(frame == 7)


@pytest.mark.parametrize("timeout_ms", [-1, 1001, True, False, 1.5, "10", None])
def test_start_rejects_invalid_native_frame_timeout(harness, timeout_ms):
    with pytest.raises(ValueError, match="frame_timeout_ms"):
        harness.camera.start(frame_timeout_ms=timeout_ms)
    assert not harness.camera.is_capturing
    assert harness.worker is None


@pytest.mark.parametrize(
    "fps,requested,expected",
    [
        (0, 0, 0),
        (0, 10, 10),
        (0, 1000, 1000),
        (60, 10, 10),
        (120, 10, 8),
        (240, 10, 4),
        (240, 1, 1),
        (2000, 10, 0),
    ],
)
def test_start_forwards_native_wait_capped_by_frame_period(
    harness, monkeypatch, fps, requested, expected
):
    monkeypatch.setattr(CaptureWorker, "start", lambda self: None)
    harness.camera.start(target_fps=fps, frame_timeout_ms=requested)
    harness.requests.put((100, 1))
    harness.worker._run_capture_cycle()
    assert harness.capture_options == [(True, expected)]


@pytest.mark.parametrize("fps", [0, 60, 120, 240])
def test_start_defaults_to_native_polling(harness, monkeypatch, fps):
    monkeypatch.setattr(CaptureWorker, "start", lambda self: None)
    harness.camera.start(target_fps=fps)
    harness.requests.put((100, 1))
    harness.worker._run_capture_cycle()
    assert harness.capture_options == [(True, 0)]
