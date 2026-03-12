from __future__ import annotations

import argparse
import ctypes
import logging
import statistics
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass, field
from typing import Any

import dxcam

logger = logging.getLogger(__name__)

ENUM_CURRENT_SETTINGS = -1
DISP_CHANGE_SUCCESSFUL = 0
DM_BITSPERPEL = 0x00040000
DM_PELSWIDTH = 0x00080000
DM_PELSHEIGHT = 0x00100000
DM_DISPLAYFREQUENCY = 0x00400000
CDS_TEST = 0x00000002
VK_MENU = 0x12
VK_RETURN = 0x0D
KEYEVENTF_KEYUP = 0x0002
INPUT_KEYBOARD = 1


class DEVMODEW(ctypes.Structure):
    _fields_ = [
        ("dmDeviceName", wintypes.WCHAR * 32),
        ("dmSpecVersion", wintypes.WORD),
        ("dmDriverVersion", wintypes.WORD),
        ("dmSize", wintypes.WORD),
        ("dmDriverExtra", wintypes.WORD),
        ("dmFields", wintypes.DWORD),
        ("dmPositionX", wintypes.LONG),
        ("dmPositionY", wintypes.LONG),
        ("dmDisplayOrientation", wintypes.DWORD),
        ("dmDisplayFixedOutput", wintypes.DWORD),
        ("dmColor", wintypes.SHORT),
        ("dmDuplex", wintypes.SHORT),
        ("dmYResolution", wintypes.SHORT),
        ("dmTTOption", wintypes.SHORT),
        ("dmCollate", wintypes.SHORT),
        ("dmFormName", wintypes.WCHAR * 32),
        ("dmLogPixels", wintypes.WORD),
        ("dmBitsPerPel", wintypes.DWORD),
        ("dmPelsWidth", wintypes.DWORD),
        ("dmPelsHeight", wintypes.DWORD),
        ("dmDisplayFlags", wintypes.DWORD),
        ("dmDisplayFrequency", wintypes.DWORD),
        ("dmICMMethod", wintypes.DWORD),
        ("dmICMIntent", wintypes.DWORD),
        ("dmMediaType", wintypes.DWORD),
        ("dmDitherType", wintypes.DWORD),
        ("dmReserved1", wintypes.DWORD),
        ("dmReserved2", wintypes.DWORD),
        ("dmPanningWidth", wintypes.DWORD),
        ("dmPanningHeight", wintypes.DWORD),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class INPUT_UNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", INPUT_UNION)]


@dataclass
class StressMetrics:
    start_time: float = field(default_factory=time.perf_counter)
    frame_count: int = 0
    none_count: int = 0
    outage_durations: list[float] = field(default_factory=list)
    transient_failure_logs: int = 0
    output_recovery_attempt_logs: int = 0
    output_recovery_success_logs: int = 0
    mode_switch_attempts: int = 0
    mode_switch_successes: int = 0
    mode_switch_failures: int = 0
    mode_switch_codes: dict[int, int] = field(default_factory=dict)
    mode_switch_timestamps: list[float] = field(default_factory=list)
    alt_enter_attempts: int = 0
    alt_enter_successes: int = 0
    alt_enter_failures: int = 0

    def add_mode_switch_code(self, code: int) -> None:
        self.mode_switch_codes[code] = self.mode_switch_codes.get(code, 0) + 1

    def elapsed(self) -> float:
        return max(0.0, time.perf_counter() - self.start_time)


class DXcamLogCounter(logging.Handler):
    def __init__(self, metrics: StressMetrics) -> None:
        super().__init__(level=logging.INFO)
        self._metrics = metrics

    def emit(self, record: logging.LogRecord) -> None:
        msg = record.getMessage()
        if "Desktop duplication transient failure" in msg:
            self._metrics.transient_failure_logs += 1
        if "Output recovery attempt" in msg:
            self._metrics.output_recovery_attempt_logs += 1
        if "Output recovery succeeded" in msg:
            self._metrics.output_recovery_success_logs += 1


def _init_devmode() -> DEVMODEW:
    dm = DEVMODEW()
    dm.dmSize = ctypes.sizeof(DEVMODEW)
    return dm


def get_current_mode(device_name: str) -> DEVMODEW:
    user32 = ctypes.windll.user32
    dm = _init_devmode()
    ok = user32.EnumDisplaySettingsW(device_name, ENUM_CURRENT_SETTINGS, ctypes.byref(dm))
    if not ok:
        raise RuntimeError("EnumDisplaySettingsW failed for current mode.")
    return dm


def enumerate_modes(device_name: str) -> list[DEVMODEW]:
    user32 = ctypes.windll.user32
    modes: list[DEVMODEW] = []
    i = 0
    while True:
        dm = _init_devmode()
        ok = user32.EnumDisplaySettingsW(device_name, i, ctypes.byref(dm))
        if not ok:
            break
        modes.append(dm)
        i += 1
    return modes


def pick_alternate_mode(current: DEVMODEW, modes: list[DEVMODEW]) -> DEVMODEW | None:
    candidates = [
        mode
        for mode in modes
        if mode.dmPelsWidth != current.dmPelsWidth
        or mode.dmPelsHeight != current.dmPelsHeight
    ]
    if not candidates:
        return None

    preferred = [
        mode
        for mode in candidates
        if mode.dmBitsPerPel == current.dmBitsPerPel
        and mode.dmDisplayFrequency == current.dmDisplayFrequency
    ]
    if preferred:
        candidates = preferred

    # Minimize disruption while still forcing mode transitions.
    candidates.sort(
        key=lambda m: abs(int(m.dmPelsWidth) - int(current.dmPelsWidth))
        + abs(int(m.dmPelsHeight) - int(current.dmPelsHeight))
    )
    return candidates[0]


def make_mode(device_name: str, source: DEVMODEW) -> DEVMODEW:
    dm = _init_devmode()
    dm.dmDeviceName = device_name
    dm.dmPelsWidth = source.dmPelsWidth
    dm.dmPelsHeight = source.dmPelsHeight
    dm.dmBitsPerPel = source.dmBitsPerPel
    dm.dmDisplayFrequency = source.dmDisplayFrequency
    dm.dmFields = DM_PELSWIDTH | DM_PELSHEIGHT | DM_BITSPERPEL | DM_DISPLAYFREQUENCY
    return dm


def apply_mode(device_name: str, mode: DEVMODEW) -> int:
    user32 = ctypes.windll.user32
    test_result = user32.ChangeDisplaySettingsExW(
        device_name,
        ctypes.byref(mode),
        None,
        CDS_TEST,
        None,
    )
    if test_result != DISP_CHANGE_SUCCESSFUL:
        return int(test_result)
    result = user32.ChangeDisplaySettingsExW(
        device_name,
        ctypes.byref(mode),
        None,
        0,
        None,
    )
    return int(result)


def _send_alt_enter() -> bool:
    user32 = ctypes.windll.user32
    inputs = (INPUT * 4)()
    inputs[0].type = INPUT_KEYBOARD
    inputs[0].u.ki = KEYBDINPUT(VK_MENU, 0, 0, 0, None)
    inputs[1].type = INPUT_KEYBOARD
    inputs[1].u.ki = KEYBDINPUT(VK_RETURN, 0, 0, 0, None)
    inputs[2].type = INPUT_KEYBOARD
    inputs[2].u.ki = KEYBDINPUT(VK_RETURN, 0, KEYEVENTF_KEYUP, 0, None)
    inputs[3].type = INPUT_KEYBOARD
    inputs[3].u.ki = KEYBDINPUT(VK_MENU, 0, KEYEVENTF_KEYUP, 0, None)
    sent = user32.SendInput(len(inputs), inputs, ctypes.sizeof(INPUT))
    return int(sent) == len(inputs)


def _window_title_for_hwnd(hwnd: int) -> str:
    user32 = ctypes.windll.user32
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, len(buf))
    return buf.value


def _find_window_by_title_substring(substring: str) -> int | None:
    user32 = ctypes.windll.user32
    target = substring.lower()
    found: list[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def enum_proc(hwnd: int, _lparam: int) -> bool:
        title = _window_title_for_hwnd(hwnd)
        if title and target in title.lower() and user32.IsWindowVisible(hwnd):
            found.append(hwnd)
            return False
        return True

    user32.EnumWindows(enum_proc, 0)
    if not found:
        return None
    return int(found[0])


def mode_switch_worker(
    *,
    stop_event: threading.Event,
    device_name: str,
    original_mode: DEVMODEW,
    alternate_mode: DEVMODEW,
    metrics: StressMetrics,
    lock: threading.Lock,
    switch_period_s: float,
    hold_s: float,
) -> None:
    while not stop_event.is_set():
        for target_mode in (alternate_mode, original_mode):
            if stop_event.is_set():
                return
            code = apply_mode(device_name, target_mode)
            with lock:
                metrics.mode_switch_attempts += 1
                metrics.add_mode_switch_code(code)
                metrics.mode_switch_timestamps.append(time.perf_counter())
                if code == DISP_CHANGE_SUCCESSFUL:
                    metrics.mode_switch_successes += 1
                else:
                    metrics.mode_switch_failures += 1
            time.sleep(hold_s)
        time.sleep(max(0.0, switch_period_s - (2.0 * hold_s)))


def exclusive_toggle_worker(
    *,
    stop_event: threading.Event,
    window_title_substring: str,
    metrics: StressMetrics,
    lock: threading.Lock,
    period_s: float,
) -> None:
    user32 = ctypes.windll.user32
    while not stop_event.is_set():
        hwnd = _find_window_by_title_substring(window_title_substring)
        with lock:
            metrics.alt_enter_attempts += 1
        if hwnd is None:
            with lock:
                metrics.alt_enter_failures += 1
            time.sleep(period_s)
            continue
        user32.SetForegroundWindow(hwnd)
        ok = _send_alt_enter()
        with lock:
            if ok:
                metrics.alt_enter_successes += 1
            else:
                metrics.alt_enter_failures += 1
        time.sleep(period_s)


def format_mode(dm: DEVMODEW) -> str:
    return (
        f"{int(dm.dmPelsWidth)}x{int(dm.dmPelsHeight)}@{int(dm.dmDisplayFrequency)} "
        f"{int(dm.dmBitsPerPel)}bpp"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Stress DXGI recovery by switching display modes while capturing."
    )
    parser.add_argument("--duration", type=float, default=30.0)
    parser.add_argument("--switch-period", type=float, default=4.0)
    parser.add_argument("--switch-hold", type=float, default=0.8)
    parser.add_argument("--backend", choices=("dxgi", "winrt"), default="dxgi")
    parser.add_argument("--processor-backend", choices=("cv2", "numpy"), default="cv2")
    parser.add_argument(
        "--outage-threshold-ms",
        type=float,
        default=20.0,
        help="Only frame gaps >= threshold are counted as outages.",
    )
    parser.add_argument(
        "--exclusive-window-title",
        default="",
        help=(
            "Optional title substring for app window to send Alt+Enter. "
            "Useful for toggling exclusive/fullscreen in target apps."
        ),
    )
    parser.add_argument("--exclusive-period", type=float, default=6.0)
    return parser.parse_args()


def summarize_metrics(metrics: StressMetrics) -> dict[str, Any]:
    elapsed = metrics.elapsed()
    outages_ms = [duration * 1000.0 for duration in metrics.outage_durations]
    return {
        "elapsed_s": elapsed,
        "frames": metrics.frame_count,
        "none_frames": metrics.none_count,
        "effective_fps": (metrics.frame_count / elapsed) if elapsed > 0 else 0.0,
        "outage_count": len(outages_ms),
        "outage_mean_ms": statistics.fmean(outages_ms) if outages_ms else 0.0,
        "outage_p95_ms": (
            statistics.quantiles(outages_ms, n=20)[18] if len(outages_ms) >= 20 else 0.0
        ),
        "outage_max_ms": max(outages_ms) if outages_ms else 0.0,
        "mode_switch_attempts": metrics.mode_switch_attempts,
        "mode_switch_successes": metrics.mode_switch_successes,
        "mode_switch_failures": metrics.mode_switch_failures,
        "mode_switch_codes": dict(sorted(metrics.mode_switch_codes.items())),
        "transient_failure_logs": metrics.transient_failure_logs,
        "recovery_attempt_logs": metrics.output_recovery_attempt_logs,
        "recovery_success_logs": metrics.output_recovery_success_logs,
        "alt_enter_attempts": metrics.alt_enter_attempts,
        "alt_enter_successes": metrics.alt_enter_successes,
        "alt_enter_failures": metrics.alt_enter_failures,
    }


def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    probe_cam = dxcam.create(output_idx=0, backend=args.backend)
    device_name = probe_cam._output.devicename
    probe_cam.release()
    current_mode = get_current_mode(device_name)
    modes = enumerate_modes(device_name)
    alternate = pick_alternate_mode(current_mode, modes)
    if alternate is None:
        raise RuntimeError("No alternate display mode found for stress test.")
    original_mode = make_mode(device_name, current_mode)
    alternate_mode = make_mode(device_name, alternate)

    logger.info("Display device: %s", device_name)
    logger.info("Original mode: %s", format_mode(original_mode))
    logger.info("Alternate mode: %s", format_mode(alternate_mode))

    metrics = StressMetrics()
    lock = threading.Lock()
    stop_event = threading.Event()

    dxcam_log_counter = DXcamLogCounter(metrics)
    logging.getLogger("dxcam").addHandler(dxcam_log_counter)

    cam = dxcam.create(
        backend=args.backend,
        processor_backend=args.processor_backend,
        output_color="BGRA",
        output_idx=0,
    )

    mode_thread = threading.Thread(
        target=mode_switch_worker,
        kwargs={
            "stop_event": stop_event,
            "device_name": device_name,
            "original_mode": original_mode,
            "alternate_mode": alternate_mode,
            "metrics": metrics,
            "lock": lock,
            "switch_period_s": args.switch_period,
            "hold_s": args.switch_hold,
        },
        daemon=True,
    )
    mode_thread.start()

    exclusive_thread = None
    if args.exclusive_window_title:
        exclusive_thread = threading.Thread(
            target=exclusive_toggle_worker,
            kwargs={
                "stop_event": stop_event,
                "window_title_substring": args.exclusive_window_title,
                "metrics": metrics,
                "lock": lock,
                "period_s": args.exclusive_period,
            },
            daemon=True,
        )
        exclusive_thread.start()
        logger.info(
            "Exclusive/fullscreen toggle enabled for title substring: %r",
            args.exclusive_window_title,
        )
    else:
        logger.info("Exclusive/fullscreen toggle disabled (no target window title).")

    outage_threshold_s = max(0.0, args.outage_threshold_ms / 1000.0)
    outage_start = None
    deadline = time.perf_counter() + args.duration

    try:
        while time.perf_counter() < deadline:
            frame = cam.grab(new_frame_only=True)
            now = time.perf_counter()
            with lock:
                if frame is None:
                    metrics.none_count += 1
                    if outage_start is None:
                        outage_start = now
                else:
                    metrics.frame_count += 1
                    if outage_start is not None:
                        outage = now - outage_start
                        if outage >= outage_threshold_s:
                            metrics.outage_durations.append(outage)
                        outage_start = None
    finally:
        stop_event.set()
        mode_thread.join(timeout=10)
        if mode_thread.is_alive():
            logger.warning("Mode switch thread did not stop cleanly.")
        if exclusive_thread is not None:
            exclusive_thread.join(timeout=5)
        apply_mode(device_name, original_mode)
        cam.release()
        logging.getLogger("dxcam").removeHandler(dxcam_log_counter)

    summary = summarize_metrics(metrics)
    logger.info("Stress summary: %s", summary)


if __name__ == "__main__":
    main()
