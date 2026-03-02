import time
import threading
from typing import Optional


class _Timer:
    def __init__(self):
        self.period_s = 0.0
        self.cancelled = False
        self._event = threading.Event()
        self._next_tick: Optional[float] = None


def create_high_resolution_timer():
    return _Timer()


def set_periodic_timer(handle: _Timer, fps: int):
    handle.period_s = 1.0 / fps
    handle.cancelled = False
    handle._event.clear()
    handle._next_tick = time.perf_counter() + handle.period_s


def wait_for_timer(handle: _Timer):
    if handle.cancelled or handle._next_tick is None:
        return
    now = time.perf_counter()
    sleep_time = handle._next_tick - now
    if sleep_time > 0:
        handle._event.wait(timeout=sleep_time)
    handle._event.clear()
    handle._next_tick += handle.period_s


def cancel_timer(handle: _Timer):
    handle.cancelled = True
    handle._event.set()
