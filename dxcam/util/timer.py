"""High-resolution Windows pacing with a separate cancellation signal."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import math
from threading import Lock
import time

_CREATE_WAITABLE_TIMER_HIGH_RESOLUTION = 0x00000002
_TIMER_ALL_ACCESS = 0x1F0003
_ERROR_INVALID_PARAMETER = 87
_INFINITE = 0xFFFFFFFF
_WAIT_OBJECT_0 = 0
_WAIT_FAILED = 0xFFFFFFFF

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.CreateWaitableTimerExW.argtypes = [
    wintypes.LPVOID,
    wintypes.LPCWSTR,
    wintypes.DWORD,
    wintypes.DWORD,
]
_kernel32.CreateWaitableTimerExW.restype = wintypes.HANDLE
_kernel32.CreateEventW.argtypes = [
    wintypes.LPVOID,
    wintypes.BOOL,
    wintypes.BOOL,
    wintypes.LPCWSTR,
]
_kernel32.CreateEventW.restype = wintypes.HANDLE
_kernel32.SetWaitableTimer.argtypes = [
    wintypes.HANDLE,
    ctypes.POINTER(ctypes.c_longlong),
    wintypes.LONG,
    wintypes.LPVOID,
    wintypes.LPVOID,
    wintypes.BOOL,
]
_kernel32.SetWaitableTimer.restype = wintypes.BOOL
_kernel32.WaitForMultipleObjects.argtypes = [
    wintypes.DWORD,
    ctypes.POINTER(wintypes.HANDLE),
    wintypes.BOOL,
    wintypes.DWORD,
]
_kernel32.WaitForMultipleObjects.restype = wintypes.DWORD
for _name in ("SetEvent", "ResetEvent", "CloseHandle"):
    _function = getattr(_kernel32, _name)
    _function.argtypes = [wintypes.HANDLE]
    _function.restype = wintypes.BOOL


def validate_target_fps(fps: int) -> None:
    """Require integer FPS, with zero reserved for unpaced capture."""
    if isinstance(fps, bool) or not isinstance(fps, int) or fps < 0:
        raise ValueError(
            "target_fps must be a nonnegative integer (0 disables pacing)."
        )
    if fps:
        try:
            1.0 / fps
        except OverflowError:
            raise ValueError("target_fps is too large.") from None


class _Timer:
    def __init__(self) -> None:
        self.period_s = 0.0
        self._next_tick: float | None = None
        self.cancelled = False
        self._lock = Lock()
        self._handle: int | None = None
        self._cancel_handle: int | None = None
        self._handle = _kernel32.CreateWaitableTimerExW(
            None, None, _CREATE_WAITABLE_TIMER_HIGH_RESOLUTION, _TIMER_ALL_ACCESS
        )
        if not self._handle and ctypes.get_last_error() == _ERROR_INVALID_PARAMETER:
            # High-resolution timers require Windows 10 1803 or later.
            self._handle = _kernel32.CreateWaitableTimerExW(
                None, None, 0, _TIMER_ALL_ACCESS
            )
        if not self._handle:
            raise ctypes.WinError(ctypes.get_last_error())
        self._cancel_handle = _kernel32.CreateEventW(None, True, False, None)
        if not self._cancel_handle:
            error = ctypes.WinError(ctypes.get_last_error())
            try:
                close_timer(self)
            finally:
                raise error

    def __del__(self) -> None:
        # Normal callers close explicitly after the waiting thread returns.
        # An active wait retains this object, so it cannot be finalized early.
        try:
            close_timer(self)
        except Exception:
            pass


def create_high_resolution_timer() -> _Timer:
    return _Timer()


def set_periodic_timer(timer: _Timer, fps: int) -> None:
    validate_target_fps(fps)
    if fps == 0:
        raise ValueError("Timer FPS must be positive.")
    with timer._lock:
        if timer._handle is None or timer._cancel_handle is None:
            raise RuntimeError("Timer is closed.")
        if not _kernel32.ResetEvent(timer._cancel_handle):
            raise ctypes.WinError(ctypes.get_last_error())
        timer.cancelled = False
        timer.period_s = 1.0 / fps
        timer._next_tick = time.perf_counter() + timer.period_s


def wait_for_timer(timer: _Timer) -> None:
    with timer._lock:
        if timer.cancelled or timer._next_tick is None:
            return
        if timer._handle is None or timer._cancel_handle is None:
            raise RuntimeError("Timer is closed.")
        sleep_s = timer._next_tick - time.perf_counter()
        if sleep_s <= 0:
            # Preserve the existing cadence: skip missed ticks rather than
            # issuing a burst of capture cycles after a long delay.
            if -sleep_s > timer.period_s:
                timer._next_tick = time.perf_counter() + timer.period_s
            else:
                timer._next_tick += timer.period_s
            return
        # Negative relative due time, rounded upward to 100-nanosecond units.
        due_time = ctypes.c_longlong(-max(1, math.ceil(sleep_s * 10_000_000)))
        if not _kernel32.SetWaitableTimer(
            timer._handle, ctypes.byref(due_time), 0, None, None, False
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        # A latched cancellation survives the arm-to-wait race. If both are
        # signaled, WaitForMultipleObjects chooses the first handle.
        handles = (wintypes.HANDLE * 2)(timer._cancel_handle, timer._handle)

    # The owner must not close either handle until this wait has returned.
    result = _kernel32.WaitForMultipleObjects(2, handles, False, _INFINITE)
    if result == _WAIT_FAILED:
        raise ctypes.WinError(ctypes.get_last_error())
    if result == _WAIT_OBJECT_0:
        return
    if result != _WAIT_OBJECT_0 + 1:
        raise RuntimeError(f"Unexpected timer wait result: {result:#x}")
    with timer._lock:
        if not timer.cancelled and timer._next_tick is not None:
            timer._next_tick += timer.period_s


def cancel_timer(timer: _Timer) -> None:
    """Wake a waiter; the owning thread closes the timer after its wait ends."""
    with timer._lock:
        timer.cancelled = True
        if timer._cancel_handle is not None:
            if not _kernel32.SetEvent(timer._cancel_handle):
                raise ctypes.WinError(ctypes.get_last_error())


def close_timer(timer: _Timer) -> None:
    """Release both handles after the owner has finished waiting."""
    with timer._lock:
        handles = (timer._handle, timer._cancel_handle)
        timer._handle = timer._cancel_handle = None
        timer._next_tick = None
        timer.cancelled = True
        error = None
        for handle in handles:
            if handle and not _kernel32.CloseHandle(handle) and error is None:
                error = ctypes.WinError(ctypes.get_last_error())
        if error is not None:
            raise error
