from __future__ import annotations

import ctypes
from threading import Event, Thread
from types import SimpleNamespace

import pytest

from dxcam.util import timer as timer_module


class FakeKernel32:
    """Model the Win32 return values and cancellation event without native waits."""

    def __init__(self, clock):
        self.clock = clock
        self.calls = []
        self.failures = {}
        self.close_errors = {}
        self.cancel_signal = Event()
        self.wait_hook = None
        self.next_handle = 101
        self.due_time = None

    def fail_once(self, name, code=5):
        self.failures.setdefault(name, []).append(code)

    def failed(self, name):
        pending = self.failures.get(name)
        if pending:
            ctypes.set_last_error(pending.pop(0))
            return True
        return False

    def handle(self):
        handle = self.next_handle
        self.next_handle += 1
        return handle

    def CreateWaitableTimerExW(self, security, name, flags, access):
        self.calls.append(("create-timer", flags))
        return 0 if self.failed("create-timer") else self.handle()

    def CreateEventW(self, security, manual_reset, initial_state, name):
        self.calls.append(("create-event", bool(manual_reset), bool(initial_state)))
        return 0 if self.failed("create-event") else self.handle()

    def ResetEvent(self, handle):
        self.calls.append(("reset", handle))
        if self.failed("reset"):
            return 0
        self.cancel_signal.clear()
        return 1

    def SetEvent(self, handle):
        self.calls.append(("signal", handle))
        if self.failed("signal"):
            return 0
        self.cancel_signal.set()
        return 1

    def SetWaitableTimer(self, handle, due_time, period, callback, argument, resume):
        due = ctypes.cast(due_time, ctypes.POINTER(ctypes.c_longlong)).contents.value
        self.calls.append(("arm", handle, due, period))
        if self.failed("arm"):
            return 0
        self.due_time = due
        return 1

    def WaitForMultipleObjects(self, count, handles, wait_all, timeout):
        self.calls.append(("wait", tuple(handles[:count]), bool(wait_all), timeout))
        if self.failed("wait"):
            return 0xFFFFFFFF
        if self.wait_hook is not None:
            return self.wait_hook()
        if self.cancel_signal.is_set():
            return 0
        self.clock.now += -self.due_time / 10_000_000
        return 1

    def CloseHandle(self, handle):
        self.calls.append(("close", handle))
        if handle in self.close_errors:
            ctypes.set_last_error(self.close_errors[handle])
            return 0
        return 1


@pytest.fixture
def native_timer(monkeypatch):
    clock = SimpleNamespace(now=10.0)
    api = FakeKernel32(clock)
    monkeypatch.setattr(timer_module, "_kernel32", api)
    monkeypatch.setattr(
        timer_module, "time", SimpleNamespace(perf_counter=lambda: clock.now)
    )
    timers = []

    def create(fps=None):
        timer = timer_module.create_high_resolution_timer()
        timers.append(timer)
        if fps is not None:
            timer_module.set_periodic_timer(timer, fps)
        return timer

    yield SimpleNamespace(api=api, clock=clock, create=create)
    api.close_errors.clear()
    for timer in timers:
        timer_module.close_timer(timer)


def calls(api, name):
    return [call for call in api.calls if call[0] == name]


def test_native_timer_uses_manual_cancel_event_and_relative_one_shot_wait(native_timer):
    timer = native_timer.create(10)
    api = native_timer.api
    assert calls(api, "create-timer") == [("create-timer", 2)]
    assert calls(api, "create-event") == [("create-event", True, False)]
    assert timer.period_s == pytest.approx(0.1)
    assert timer._next_tick == pytest.approx(10.1)

    timer_module.wait_for_timer(timer)

    arm = calls(api, "arm")[0]
    assert arm[1] == timer._handle
    assert arm[2] == pytest.approx(-1_000_000, abs=1)
    assert arm[3] == 0  # Each wait uses its own deadline, not a native periodic timer.
    assert calls(api, "wait") == [
        ("wait", (timer._cancel_handle, timer._handle), False, 0xFFFFFFFF)
    ]
    assert timer._next_tick == pytest.approx(10.2)


def test_sub_100ns_remaining_delay_never_becomes_an_absolute_due_time(native_timer):
    timer = native_timer.create(10)
    timer._next_tick = native_timer.clock.now + 0.00000001
    timer_module.wait_for_timer(timer)
    assert calls(native_timer.api, "arm")[0][2] == -1


@pytest.mark.parametrize(
    "now,next_tick", [(10.1, 10.2), (10.125, 10.2), (10.35, 10.45)]
)
def test_late_capture_advances_or_drops_missed_ticks_without_catchup_waits(
    native_timer, now, next_tick
):
    timer = native_timer.create(10)
    native_timer.clock.now = now
    timer_module.wait_for_timer(timer)
    assert timer._next_tick == pytest.approx(next_tick)
    assert not calls(native_timer.api, "arm")
    assert not calls(native_timer.api, "wait")
    # The next iteration must pace to a future deadline, not produce a burst.
    timer_module.wait_for_timer(timer)
    assert len(calls(native_timer.api, "wait")) == 1
    assert calls(native_timer.api, "arm")[0][2] < 0


def test_cancel_before_arm_prevents_wait_and_reconfiguration_resets_event(native_timer):
    timer = native_timer.create(10)
    timer_module.cancel_timer(timer)
    timer_module.wait_for_timer(timer)
    assert timer.cancelled
    assert not calls(native_timer.api, "arm")
    assert not calls(native_timer.api, "wait")

    timer_module.set_periodic_timer(timer, 20)
    assert not timer.cancelled
    assert not native_timer.api.cancel_signal.is_set()
    timer_module.wait_for_timer(timer)
    assert timer._next_tick == pytest.approx(10.1)
    assert len(calls(native_timer.api, "wait")) == 1


def test_cancel_between_arming_and_native_wait_wins_over_ready_tick(native_timer):
    timer = native_timer.create(10)
    deadline = timer._next_tick

    def begin_wait():
        # The native wait cannot hold the mutex required by cancellation.
        assert timer._lock.acquire(blocking=False)
        timer._lock.release()
        timer_module.cancel_timer(timer)
        handles = calls(native_timer.api, "wait")[-1][1]
        assert handles[0] == timer._cancel_handle
        return 0  # Both signals ready: Win32 returns the first handle's index.

    native_timer.api.wait_hook = begin_wait
    timer_module.wait_for_timer(timer)
    assert timer.cancelled
    assert timer._next_tick == deadline
    assert not calls(native_timer.api, "close")


def test_cancel_wakes_an_active_native_wait_without_closing_handles(native_timer):
    timer = native_timer.create(1)
    entered, finished = Event(), Event()
    errors = []

    def blocking_wait():
        entered.set()
        assert native_timer.api.cancel_signal.wait(5)
        return 0

    def wait():
        try:
            timer_module.wait_for_timer(timer)
        except BaseException as error:
            errors.append(error)
        finally:
            finished.set()

    native_timer.api.wait_hook = blocking_wait
    thread = Thread(target=wait)
    canceller = Thread(target=timer_module.cancel_timer, args=(timer,))
    thread.start()
    try:
        assert entered.wait(5)
        canceller.start()
        assert finished.wait(2)
        thread.join(5)
        canceller.join(5)
        assert not canceller.is_alive()
        assert not errors
        assert not calls(native_timer.api, "close")
    finally:
        native_timer.api.cancel_signal.set()
        thread.join(5)
        if canceller.ident is not None:
            canceller.join(5)


@pytest.mark.parametrize("operation", ["reset", "arm", "wait"])
def test_failed_native_timer_operations_raise_and_still_allow_cleanup(
    native_timer, operation
):
    timer = native_timer.create()
    native_timer.api.fail_once(operation)
    with pytest.raises(OSError):
        timer_module.set_periodic_timer(timer, 10)
        timer_module.wait_for_timer(timer)
    if operation == "arm":
        assert not calls(native_timer.api, "wait")
    timer_module.close_timer(timer)
    assert len(calls(native_timer.api, "close")) == 2


def test_failed_cancel_is_retriable_and_keeps_handles_alive(native_timer):
    timer = native_timer.create(10)
    native_timer.api.fail_once("signal")
    with pytest.raises(OSError):
        timer_module.cancel_timer(timer)
    assert timer.cancelled
    assert timer._handle is not None and timer._cancel_handle is not None
    timer_module.cancel_timer(timer)
    assert native_timer.api.cancel_signal.is_set()
    assert len(calls(native_timer.api, "signal")) == 2
    assert not calls(native_timer.api, "close")


@pytest.mark.parametrize("operation", ["create-timer", "create-event"])
def test_creation_failure_closes_any_successfully_created_handle(
    native_timer, operation
):
    native_timer.api.fail_once(operation)
    with pytest.raises(OSError) as error:
        native_timer.create()
    assert error.value.winerror == 5
    assert len(calls(native_timer.api, "close")) == int(operation == "create-event")


def test_event_creation_error_survives_failed_partial_cleanup(native_timer):
    native_timer.api.fail_once("create-event", 5)
    native_timer.api.close_errors[101] = 6
    with pytest.raises(OSError) as error:
        native_timer.create()
    assert error.value.winerror == 5
    assert calls(native_timer.api, "close") == [("close", 101)]


def test_high_resolution_flag_falls_back_only_when_unsupported(native_timer):
    native_timer.api.fail_once("create-timer", 87)
    timer = native_timer.create()
    assert timer._handle is not None
    assert calls(native_timer.api, "create-timer") == [
        ("create-timer", 2),
        ("create-timer", 0),
    ]


def test_close_attempts_both_handles_preserves_first_error_and_is_idempotent(
    native_timer,
):
    timer = native_timer.create(10)
    handles = (timer._handle, timer._cancel_handle)
    native_timer.api.close_errors.update({handles[0]: 5, handles[1]: 6})
    with pytest.raises(OSError) as error:
        timer_module.close_timer(timer)
    assert error.value.winerror == 5
    assert calls(native_timer.api, "close") == [("close", handle) for handle in handles]
    assert timer._handle is None and timer._cancel_handle is None
    timer_module.close_timer(timer)
    timer_module.cancel_timer(timer)
    assert len(calls(native_timer.api, "close")) == 2
    assert not calls(native_timer.api, "signal")


@pytest.mark.parametrize("fps", [0, -1, True, False, 1.0, "60", None])
def test_invalid_timer_fps_cannot_change_existing_schedule(native_timer, fps):
    timer = native_timer.create(10)
    before = (
        timer.period_s,
        timer._next_tick,
        timer.cancelled,
        list(native_timer.api.calls),
    )
    with pytest.raises(ValueError):
        timer_module.set_periodic_timer(timer, fps)
    assert (
        timer.period_s,
        timer._next_tick,
        timer.cancelled,
        native_timer.api.calls,
    ) == before
