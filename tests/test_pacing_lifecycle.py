from __future__ import annotations

import importlib
from threading import Event, Lock, Thread
from types import SimpleNamespace

import pytest

from dxcam.dxcam import DXCamera
from dxcam.runtime.capture_worker import CaptureWorker
from dxcam.runtime.frame_buffer import FrameBuffer


class TimerHooks:
    """Block only at explicit barriers; no native timers or pacing sleeps."""

    def __init__(self):
        self.created = []
        self.calls = []
        self.entered = Event()
        self.create_hook = None
        self.setup_hook = None
        self.wait_hook = None
        self.cancel_error = None
        self.close_error = None

    def create(self):
        timer = SimpleNamespace(wake=Event(), waiting=False, closed=False)
        self.created.append(timer)
        self.calls.append("create")
        if self.create_hook:
            self.create_hook(timer)
        return timer

    def setup(self, timer, fps):
        assert not timer.closed
        self.calls.append("setup")
        if self.setup_hook:
            self.setup_hook(timer)

    def wait(self, timer):
        assert not timer.closed
        timer.waiting = True
        self.calls.append("wait")
        self.entered.set()
        try:
            if self.wait_hook:
                self.wait_hook(timer)
            else:
                assert timer.wake.wait(5), "Cancellation did not wake timer wait"
        finally:
            timer.waiting = False
            self.calls.append("wait-return")

    def cancel(self, timer):
        assert not timer.closed, "Cancellation raced with closed handles"
        self.calls.append("cancel")
        timer.wake.set()
        if self.cancel_error:
            raise self.cancel_error

    def close(self, timer):
        assert not timer.waiting, "Handles closed while a wait still owned them"
        assert not timer.closed, "Timer closed more than once"
        timer.closed = True
        self.calls.append("close")
        if self.close_error:
            raise self.close_error


@pytest.fixture
def timer_hooks(monkeypatch):
    module = importlib.import_module("dxcam.runtime.capture_worker")
    hooks = TimerHooks()
    monkeypatch.setattr(module, "create_high_resolution_timer", hooks.create)
    monkeypatch.setattr(module, "set_periodic_timer", hooks.setup)
    monkeypatch.setattr(module, "wait_for_timer", hooks.wait)
    monkeypatch.setattr(module, "cancel_timer", hooks.cancel)
    monkeypatch.setattr(module, "close_timer", hooks.close)
    return hooks


def make_worker():
    worker = CaptureWorker(
        FrameBuffer(), Lock(), lambda *args: None, lambda: (0, 0, 2, 2), target_fps=1
    )
    captures = []
    worker._run_capture_cycle = lambda: captures.append("capture")
    return worker, captures


def finish_worker(worker, hooks):
    # Release fake waits even when the assertion under test failed.
    for timer in hooks.created:
        timer.wake.set()
    try:
        worker.stop()
    except OSError:
        pass
    assert worker.join(5), "Worker did not finish after test cleanup"


def test_stop_before_run_never_captures_or_leaks_timer(timer_hooks):
    worker, captures = make_worker()
    worker.stop()
    worker.run_loop()
    assert not captures
    assert "wait" not in timer_hooks.calls
    assert all(timer.closed for timer in timer_hooks.created)
    assert worker.consume_error() is None


@pytest.mark.parametrize("phase", ["create", "setup"])
def test_stop_during_timer_initialization_prevents_first_wait(timer_hooks, phase):
    entered, resume = Event(), Event()

    def pause(timer):
        entered.set()
        assert resume.wait(5)

    setattr(timer_hooks, f"{phase}_hook", pause)
    worker, captures = make_worker()
    stopper = Thread(target=worker.stop)
    worker.start()
    try:
        assert entered.wait(5)
        stopper.start()
        assert worker.stop_event.wait(5)
        resume.set()
        stopper.join(5)
        assert not stopper.is_alive()
        assert worker.join(5)
        assert not captures
        assert "wait" not in timer_hooks.calls
        assert len(timer_hooks.created) == 1
        assert timer_hooks.created[0].closed
        assert worker.consume_error() is None
    finally:
        resume.set()
        finish_worker(worker, timer_hooks)
        if stopper.ident is not None:
            stopper.join(5)


def test_stop_wakes_wait_before_close_and_worker_can_restart(timer_hooks):
    worker, captures = make_worker()
    try:
        for _ in range(2):
            timer_hooks.entered.clear()
            worker.start()
            assert timer_hooks.entered.wait(5)
            worker.stop()
            assert worker.join(5)
            assert timer_hooks.created[-1].closed
            assert worker.consume_error() is None
        assert len(timer_hooks.created) == 2
        assert timer_hooks.created[0] is not timer_hooks.created[1]
        assert timer_hooks.calls.count("close") == 2
        assert not captures
    finally:
        finish_worker(worker, timer_hooks)


def test_stop_during_owner_close_cannot_signal_detached_handles(
    timer_hooks, monkeypatch
):
    module = importlib.import_module("dxcam.runtime.capture_worker")
    worker, _ = make_worker()
    closing, resume, stopped = Event(), Event(), Event()
    stop_errors = []

    def pause_close(timer):
        timer_hooks.close(timer)
        closing.set()
        assert resume.wait(5)

    def stop():
        try:
            worker.stop()
        except BaseException as error:
            stop_errors.append(error)
        finally:
            stopped.set()

    monkeypatch.setattr(module, "close_timer", pause_close)
    timer_hooks.wait_hook = lambda timer: worker.stop_event.set()
    stopper = Thread(target=stop)
    worker.start()
    try:
        assert closing.wait(5)
        cancellations_before = timer_hooks.calls.count("cancel")
        stopper.start()
        assert stopped.wait(2)
        assert not stop_errors
        assert timer_hooks.calls.count("cancel") == cancellations_before
    finally:
        resume.set()
        finish_worker(worker, timer_hooks)
        if stopper.ident is not None:
            stopper.join(5)
    assert worker.consume_error() is None


def condition_waiter(worker, condition):
    ready, done = Event(), Event()

    def wait():
        with condition:
            ready.set()
            condition.wait_for(lambda: worker.stopped, timeout=5)
        done.set()

    thread = Thread(target=wait)
    thread.start()
    assert ready.wait(5)
    return thread, done


@pytest.mark.parametrize("phase", ["create", "setup", "wait", "capture"])
def test_primary_worker_error_survives_cleanup_and_wakes_waiters(timer_hooks, phase):
    worker, _ = make_worker()
    primary = RuntimeError(f"{phase} failed")

    def fail(*args):
        raise primary

    if phase == "capture":
        timer_hooks.wait_hook = lambda timer: None
        worker._run_capture_cycle = fail
    else:
        setattr(timer_hooks, f"{phase}_hook", fail)
    timer_hooks.cancel_error = OSError("cancel failed")
    timer_hooks.close_error = OSError("close failed")
    waiters = [
        condition_waiter(worker, worker.frame_condition),
        condition_waiter(worker, worker.capacity_condition),
    ]
    try:
        worker.run_loop()
        assert worker.consume_error() is primary
        assert worker.stopped
        for thread, done in waiters:
            assert done.wait(2), "Cleanup failed to notify a waiting consumer"
            thread.join(5)
        # A failed create owns its own partial-resource cleanup. Every returned
        # timer must be closed even when setup, cancellation, or capture fails.
        if phase != "create":
            assert all(timer.closed for timer in timer_hooks.created)
    finally:
        for condition in (worker.frame_condition, worker.capacity_condition):
            with condition:
                condition.notify_all()
        for thread, _ in waiters:
            thread.join(5)


@pytest.mark.parametrize("cleanup", ["cancel", "close"])
def test_cleanup_error_is_reported_when_there_is_no_primary_error(timer_hooks, cleanup):
    worker, _ = make_worker()
    error = OSError(f"{cleanup} failed")
    setattr(timer_hooks, f"{cleanup}_error", error)
    timer_hooks.wait_hook = lambda timer: worker.stop_event.set()
    worker.run_loop()
    assert worker.consume_error() is error
    assert worker.stopped
    assert timer_hooks.created[0].closed


@pytest.fixture
def start_probe(monkeypatch):
    module = importlib.import_module("dxcam.dxcam")
    events = []
    camera = DXCamera.__new__(DXCamera)
    camera._is_released = False
    camera.is_capturing = False
    camera.width, camera.height = 13, 9
    camera.region = (0, 0, 13, 9)
    camera._region_set_by_user = False
    camera._DXCamera__worker = None
    camera._DXCamera__lock = Lock()
    camera._DXCamera__frame_buffer = FrameBuffer()
    camera._DXCamera__last_grab_entry = object()
    camera._duplicator = SimpleNamespace(
        reset_frame_tracking=lambda: events.append("reset")
    )
    camera._allocate_capture_slots_for_region = lambda *a, **kw: events.append(
        "allocate"
    )
    camera._recover_output = lambda: events.append("recover")
    monkeypatch.setattr(module.time, "sleep", lambda delay: events.append("sleep"))

    class StartupWorker:
        def __init__(self, **kwargs):
            self.target_fps = kwargs["target_fps"]
            events.append("construct-worker")

        def start(self):
            events.append("start")

    monkeypatch.setattr(module, "CaptureWorker", StartupWorker)
    try:
        yield camera, events
    finally:
        # This deliberately synthetic camera never owns native resources.
        camera._is_released = True


@pytest.mark.parametrize(
    "fps", [-1, True, False, 1.0, 0.0, "60", None, float("inf"), float("nan")]
)
def test_invalid_fps_is_rejected_before_start_side_effects(start_probe, fps):
    camera, events = start_probe
    original_region = camera.region
    original_cache = camera._DXCamera__last_grab_entry
    with pytest.raises(ValueError, match="target_fps"):
        camera.start(region=(1, 1, 8, 7), target_fps=fps, delay=9)
    assert not events
    assert camera.region == original_region
    assert not camera._region_set_by_user
    assert camera._DXCamera__worker is None
    assert camera._DXCamera__last_grab_entry is original_cache
    assert not camera.is_capturing


@pytest.mark.parametrize("fps", [0, 1, 60, 240])
def test_nonnegative_integer_fps_starts_capture(start_probe, fps):
    camera, events = start_probe
    camera.start(target_fps=fps)
    assert camera.is_capturing
    assert camera._DXCamera__worker.target_fps == fps
    assert events == ["allocate", "reset", "construct-worker", "start"]


@pytest.mark.parametrize("primary_capture_error", [False, True])
def test_camera_stop_signaling_error_still_joins_and_cleans_session(
    primary_capture_error,
):
    from test_capture_lifecycle import make_camera

    camera, stages = make_camera()
    worker = camera._DXCamera__worker
    stop_error = OSError("SetEvent failed")
    capture_error = RuntimeError("capture failed") if primary_capture_error else None
    worker.error = capture_error
    original_stop, original_join = worker.stop, worker.join
    joins = []
    camera._DXCamera__last_grab_entry = object()

    def failing_stop():
        original_stop()
        raise stop_error

    def join(timeout):
        joins.append(timeout)
        return original_join(timeout)

    worker.stop, worker.join = failing_stop, join
    try:
        with pytest.raises((RuntimeError, OSError)) as error:
            camera.stop()
        assert error.value is (capture_error if primary_capture_error else stop_error)
        assert joins == [10]
        assert not worker.running
        assert not camera.is_capturing
        assert camera._DXCamera__worker is None
        assert camera._DXCamera__last_grab_entry is None
        assert not camera._DXCamera__frame_buffer.slots
        assert [stage.releases for stage in stages] == [1, 1, 1]
    finally:
        worker.stop = original_stop
        camera.release()


def test_camera_stop_signaling_error_and_failed_join_keep_live_resources():
    from test_capture_lifecycle import make_camera

    camera, stages = make_camera()
    worker = camera._DXCamera__worker
    worker.join_ok = False
    stop_error = OSError("SetEvent failed")
    original_stop = worker.stop
    cache = camera._DXCamera__last_grab_entry = object()

    def failing_stop():
        original_stop()
        raise stop_error

    worker.stop = failing_stop
    try:
        with pytest.raises(RuntimeError, match="did not stop") as error:
            camera.stop()
        assert error.value.__cause__ is stop_error
        assert camera.is_capturing
        assert camera._DXCamera__worker is worker
        assert camera._DXCamera__last_grab_entry is cache
        assert len(camera._DXCamera__frame_buffer.slots) == 3
        assert [stage.releases for stage in stages] == [0, 0, 0]
        assert camera._stagesurf.releases == 0
    finally:
        worker.stop = original_stop
        worker.join_ok = True
        camera.release()
