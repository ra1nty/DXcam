import sys
import time
import ctypes
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
        now = time.perf_counter()
        sleep_s = timer._next_tick - now
        if sleep_s > 0:
            time.sleep(sleep_s)
            timer._next_tick += timer.period_s
            return
        # If we are late by more than one period, drop missed ticks
        # instead of bursting catch-up iterations.
        if -sleep_s > timer.period_s:
            timer._next_tick = time.perf_counter() + timer.period_s
            return
        timer._next_tick += timer.period_s

    def cancel_timer(_timer: _Timer):
        pass

else:
    # On Python < 3.11, time.sleep() on Windows has ~15ms resolution.
    # Use CreateWaitableTimerExW with CREATE_WAITABLE_TIMER_HIGH_RESOLUTION
    # (available on Windows 10 1803+) for sub-millisecond precision.
    # Each wait uses a fresh negative LARGE_INTEGER due time (relative, in 100ns
    # intervals) computed from perf_counter(), so drift never accumulates.

    _kernel32 = ctypes.windll.kernel32

    _CREATE_WAITABLE_TIMER_HIGH_RESOLUTION = 0x00000002
    _TIMER_ALL_ACCESS = 0x1F0003
    _INFINITE = 0xFFFFFFFF

    _kernel32.CreateWaitableTimerExW.restype = ctypes.c_void_p
    _kernel32.SetWaitableTimer.restype = ctypes.c_long
    _kernel32.WaitForSingleObject.restype = ctypes.c_ulong
    _kernel32.CancelWaitableTimer.restype = ctypes.c_long
    _kernel32.CloseHandle.restype = ctypes.c_long

    class _TimerLegacy(_Timer):
        def __init__(self):
            super().__init__()
            self.cancelled = False
            self._handle = _kernel32.CreateWaitableTimerExW(
                None,
                None,
                _CREATE_WAITABLE_TIMER_HIGH_RESOLUTION,
                _TIMER_ALL_ACCESS,
            )
            if not self._handle:
                raise ctypes.WinError(ctypes.get_last_error())

        def __del__(self):
            if self._handle:
                _kernel32.CloseHandle(self._handle)
                self._handle = None

    def create_high_resolution_timer() -> _Timer:
        return _TimerLegacy()

    def set_periodic_timer(timer: _Timer, fps: int):
        timer.period_s = 1.0 / fps
        timer.cancelled = False
        timer._next_tick = time.perf_counter() + timer.period_s

    def wait_for_timer(timer: _Timer):
        if timer.cancelled or timer._next_tick is None:
            return
        now = time.perf_counter()
        sleep_s = timer._next_tick - now
        if sleep_s > 0:
            # Negative value = relative time in 100-nanosecond intervals.
            due_time = ctypes.c_longlong(-int(sleep_s * 10_000_000))
            _kernel32.SetWaitableTimer(
                timer._handle, ctypes.byref(due_time), 0, None, None, 0
            )
            _kernel32.WaitForSingleObject(timer._handle, _INFINITE)
            if not timer.cancelled:
                timer._next_tick += timer.period_s
            return
        if not timer.cancelled:
            if -sleep_s > timer.period_s:
                timer._next_tick = time.perf_counter() + timer.period_s
                return
            timer._next_tick += timer.period_s

    def cancel_timer(timer: _Timer):
        timer.cancelled = True
        # Signal the timer immediately so WaitForSingleObject returns.
        due_time = ctypes.c_longlong(0)
        _kernel32.SetWaitableTimer(
            timer._handle, ctypes.byref(due_time), 0, None, None, 0
        )
