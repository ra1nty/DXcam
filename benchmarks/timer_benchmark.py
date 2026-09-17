"""
Benchmark dxcam.util.timer at several target FPS values.

Reports actual FPS, mean error, and max jitter. The current timer uses native
Windows waits on every supported Python version. Use --timer-path to compare
older implementations, or compare_timer_pacing.py for paired measurements.
"""

import logging
import sys
import time
import statistics

import argparse
import importlib.util as _ilu
import os as _os

_args, _ = argparse.ArgumentParser().parse_known_args()
_default = _os.path.join(_os.path.dirname(__file__), "..", "dxcam", "util", "timer.py")
_parser = argparse.ArgumentParser()
_parser.add_argument("--timer-path", default=_default)
_path = _parser.parse_args().timer_path
_spec = _ilu.spec_from_file_location("timer", _path)
assert _spec and _spec.loader
_mod = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
create_high_resolution_timer = _mod.create_high_resolution_timer
set_periodic_timer = _mod.set_periodic_timer
wait_for_timer = _mod.wait_for_timer
cancel_timer = _mod.cancel_timer
close_timer = getattr(_mod, "close_timer", None)

DURATION_S = 5
WARMUP_TICKS = 10
TARGET_FPS_LIST = [60, 120, 240]

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def run(fps: int) -> list:
    logger.info("Running timer benchmark for target fps=%d", fps)
    timer = create_high_resolution_timer()
    try:
        set_periodic_timer(timer, fps)
        period_s = 1.0 / fps
        intervals = []
        last = time.perf_counter()
        deadline = last + WARMUP_TICKS * period_s + DURATION_S
        tick = 0
        while time.perf_counter() < deadline:
            wait_for_timer(timer)
            now = time.perf_counter()
            if tick >= WARMUP_TICKS:
                intervals.append(now - last)
            last = now
            tick += 1
            if tick % (fps or 1) == 0:
                logger.debug("fps=%d progressed to tick=%d", fps, tick)
        return intervals
    finally:
        try:
            cancel_timer(timer)
        finally:
            if close_timer is not None:
                close_timer(timer)


def report(fps: int, intervals: list):
    target_s = 1.0 / fps
    errors = [abs(iv - target_s) * 1000 for iv in intervals]
    actual_fps = len(intervals) / sum(intervals)
    logger.info(
        "target=%3d fps  actual=%7.3f fps  mean_err=%.3f ms  max_err=%.3f ms",
        fps,
        actual_fps,
        statistics.mean(errors),
        max(errors),
    )


if __name__ == "__main__":
    logger.info("Python %s", sys.version)
    logger.info(
        "Duration: %ss per target, warmup: %d ticks",
        DURATION_S,
        WARMUP_TICKS,
    )
    for fps in TARGET_FPS_LIST:
        report(fps, run(fps))
