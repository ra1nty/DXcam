import sys
import time
import threading
from typing import Optional


class _Timer:
    def __init__(self):
        self.period_s: float = 0.0
        self._next_tick: Optional[float] = None


if sys.version_info >= (3, 11):
    # time.sleep() uses CreateWaitableTimerExW with CREATE_WAITABLE_TIMER_HIGH_RESOLUTION
    # internally on Windows since Python 3.11, so no manual WinAPI calls needed.

    def create_high_resolution_timer() -> _Timer:
        return _Timer()

    def set_periodic_timer(timer: _Timer, fps: int):
        timer.period_s = 1.0 / fps
        timer._next_tick = time.perf_counter() + timer.period_s

    def wait_for_timer(timer: _Timer):
        if timer._next_tick is None:
            return
        sleep_s = timer._next_tick - time.perf_counter()
        if sleep_s > 0:
            time.sleep(sleep_s)
        timer._next_tick += timer.period_s

    def cancel_timer(_timer: _Timer):
        pass

else:
    # On Python < 3.11, time.sleep() on Windows has ~15ms resolution.
    # threading.Event.wait() goes through lock acquisition (WaitForSingleObjectEx)
    # which is also limited by system timer resolution. Use threading.Event anyway
    # for interruptible waits; precision is best-effort on older runtimes.

    class _TimerLegacy(_Timer):
        def __init__(self):
            super().__init__()
            self._event = threading.Event()
            self.cancelled = False

    def create_high_resolution_timer() -> _Timer:
        return _TimerLegacy()

    def set_periodic_timer(timer: _Timer, fps: int):
        timer.period_s = 1.0 / fps
        timer.cancelled = False
        timer._event.clear()
        timer._next_tick = time.perf_counter() + timer.period_s

    def wait_for_timer(timer: _Timer):
        if timer.cancelled or timer._next_tick is None:
            return
        sleep_s = timer._next_tick - time.perf_counter()
        if sleep_s > 0:
            timer._event.wait(timeout=sleep_s)
        timer._event.clear()
        timer._next_tick += timer.period_s

    def cancel_timer(timer: _Timer):
        timer.cancelled = True
        timer._event.set()
