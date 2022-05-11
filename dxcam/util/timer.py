import ctypes
from ctypes.wintypes import LARGE_INTEGER


INFINITE = 0xFFFFFFFF
WAIT_FAILED = 0xFFFFFFFF
CREATE_WAITABLE_TIMER_HIGH_RESOLUTION = 0x00000002
TIMER_MODIFY_STATE = 0x0002
TIMER_ALL_ACCESS = 0x1F0003


__kernel32 = ctypes.windll.kernel32


def create_high_resolution_timer():
    handle = __kernel32.CreateWaitableTimerExW(
        None, None, CREATE_WAITABLE_TIMER_HIGH_RESOLUTION, TIMER_ALL_ACCESS
    )
    if handle == 0:
        raise ctypes.WinError()
    return handle


def set_periodic_timer(handle, period: int):
    res = __kernel32.SetWaitableTimer(
        handle,
        ctypes.byref(LARGE_INTEGER(0)),
        period,
        None,
        None,
        0,
    )
    if res == 0:
        raise ctypes.WinError()
    return True


wait_for_timer = __kernel32.WaitForSingleObject
cancel_timer = __kernel32.CancelWaitableTimer
