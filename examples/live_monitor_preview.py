"""Live preview of your monitor or one application window, with FPS in the title.

**Simple usage**

Install once::

    pip install opencv-python
    pip install psutil "dxcam[winrt]"   # only if you use --process

Examples::

    # Whole monitor, preview at most 1280x720 (default caps)
    python examples/live_monitor_preview.py

    # Capture Notepad's window only
    python examples/live_monitor_preview.py --process notepad.exe

    # Full-size preview (no scaling)
    python examples/live_monitor_preview.py --max-width 0 --max-height 0

    # Larger preview area (keep the same aspect ratio as your monitor for undistorted image)
    python examples/live_monitor_preview.py --max-width 1920 --max-height 1080

The preview keeps the **same aspect ratio** as the capture; it is only scaled down to
fit inside ``--max-width`` / ``--max-height``. The window uses **pixel-accurate** sizing
(no stretch) and **area** resampling when shrinking so text stays sharper than linear blur.

Press **q** in the preview window to quit.

Needs a normal **opencv-python** build (with GUI), not opencv-python-headless.
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass
from importlib import import_module
from typing import Any, cast

import dxcam
from dxcam.types import CaptureBackend
from dxcam.util.hwnd import enumerate_hwnds_for_pid, is_window_valid, pick_largest_visible_hwnd

# Preview title / FPS tuning (not exposed as CLI to keep the script small).
_TITLE_FPS_EVERY_N_FRAMES = 15
_FPS_ROLLING_SAMPLES = 60


@dataclass(frozen=True)
class _OpenCVInfo:
    module: Any | None
    processor_ok: bool
    gui_ok: bool
    gui_diagnostic: str


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Live DXcam preview with optional window capture and preview size limits.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument(
        "-p",
        "--process",
        metavar="EXE",
        default=None,
        help="App to capture, e.g. notepad or notepad.exe. Omit for full monitor. "
        "Requires: pip install psutil  and  pip install \"dxcam[winrt]\"",
    )
    p.add_argument(
        "--max-width",
        type=int,
        default=1280,
        metavar="W",
        help="Maximum preview width in pixels (default: 1280). Use 0 for no width cap.",
    )
    p.add_argument(
        "--max-height",
        type=int,
        default=720,
        metavar="H",
        help="Maximum preview height in pixels (default: 720). Use 0 for no height cap.",
    )

    adv = p.add_argument_group("advanced")
    adv.add_argument(
        "--backend",
        choices=("dxgi", "winrt"),
        default="dxgi",
        help="Capture backend when not using --process (default: dxgi). "
        "Window capture always uses winrt.",
    )
    adv.add_argument(
        "--device-idx",
        type=int,
        default=0,
        help="GPU adapter index (default: 0).",
    )
    adv.add_argument(
        "--output-idx",
        type=int,
        default=None,
        metavar="N",
        help="Monitor index on the adapter (default: primary).",
    )
    adv.add_argument(
        "--target-fps",
        type=int,
        default=0,
        help="Capture thread pacing; 0 = uncapped (default).",
    )
    adv.add_argument(
        "--video-mode",
        action="store_true",
        help="Reuse last frame when the desktop has no new updates.",
    )
    adv.add_argument(
        "--pid",
        type=int,
        default=None,
        metavar="N",
        help="Instead of --process: capture largest visible window of this PID.",
    )
    adv.add_argument(
        "--hwnd",
        type=int,
        default=None,
        metavar="N",
        help="Instead of --process: capture this window handle (HWND).",
    )
    return p.parse_args()


def _preview_resize(frame: Any, max_w: int, max_h: int, cv2: Any) -> Any:
    """Scale down to fit inside max_w x max_h; preserve aspect ratio; never upscale.

    Uses INTER_AREA for decimation (sharper than INTER_LINEAR for UI/text).
    """
    if max_w <= 0 and max_h <= 0:
        return frame
    h, w = frame.shape[:2]
    limit_w = max_w if max_w > 0 else 10**9
    limit_h = max_h if max_h > 0 else 10**9
    scale = min(limit_w / w, limit_h / h, 1.0)
    if scale >= 1.0:
        return frame
    nw = max(1, int(round(w * scale)))
    nh = max(1, int(round(h * scale)))
    return cv2.resize(frame, (nw, nh), interpolation=cv2.INTER_AREA)


def _analyze_opencv() -> _OpenCVInfo:
    try:
        cv2 = import_module("cv2")
    except ImportError as exc:
        return _OpenCVInfo(
            None,
            False,
            False,
            f"cv2 import failed ({exc}). Install: pip install opencv-python",
        )

    processor_ok = hasattr(cv2, "cvtColor") and hasattr(cv2, "COLOR_BGRA2RGB")
    cv2_path = (getattr(cv2, "__file__", "") or "").replace("\\", "/").lower()
    headless_wheel = "headless" in cv2_path

    if not hasattr(cv2, "namedWindow"):
        diag = (
            "cv2 has no namedWindow (wrong package or a local cv2.py shadowing opencv). "
            "Fix: pip install opencv-python"
        )
        return _OpenCVInfo(cv2, processor_ok, False, diag)

    try:
        cv2.namedWindow("_dxcam_gui_probe", cv2.WINDOW_AUTOSIZE)
        cv2.destroyAllWindows()
    except Exception as exc:
        msg = str(exc).lower()
        lines: list[str] = []
        if headless_wheel:
            lines.append("opencv-python-headless has no GUI. Use: pip install opencv-python")
        elif "not implemented" in msg or "gui" in msg:
            lines.append("This OpenCV build has no GUI. Try: pip install opencv-python")
        lines.append(f"Probe error: {exc}")
        return _OpenCVInfo(cv2, processor_ok, False, "\n".join(lines))

    return _OpenCVInfo(cv2, processor_ok, True, "")


def _normalize_exe_name(name: str) -> str:
    n = name.lower().strip()
    return n if n.endswith(".exe") else f"{n}.exe"


def _resolve_capture_hwnd(args: argparse.Namespace) -> tuple[int | None, str]:
    has_hwnd = args.hwnd is not None
    has_pid = args.pid is not None
    has_process = bool(args.process and str(args.process).strip())
    if sum((has_hwnd, has_pid, has_process)) > 1:
        print("Use only one of --process, --pid, or --hwnd.", file=sys.stderr)
        sys.exit(2)

    if args.hwnd is not None:
        hwnd = int(args.hwnd)
        if not is_window_valid(hwnd):
            print(f"Invalid or closed window handle: {hwnd}", file=sys.stderr)
            sys.exit(1)
        return hwnd, f"hwnd={hwnd}"

    if args.pid is not None:
        pid = int(args.pid)
        hwnd = pick_largest_visible_hwnd(pid)
        if hwnd is None:
            hwnds = enumerate_hwnds_for_pid(pid)
            if not hwnds:
                print(f"No visible top-level window for PID {pid}.", file=sys.stderr)
                sys.exit(1)
            hwnd = hwnds[0]
        return hwnd, f"pid={pid}"

    if args.process is not None:
        try:
            import psutil
        except ImportError:
            print("--process needs psutil: pip install psutil", file=sys.stderr)
            sys.exit(1)
        exe = _normalize_exe_name(str(args.process))
        pids: list[int] = []
        for proc in psutil.process_iter(["name", "pid"]):
            try:
                pname = proc.info.get("name")
                if pname and pname.lower() == exe:
                    pids.append(int(proc.info["pid"]))
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        if not pids:
            print(f"No running process named {exe!r}.", file=sys.stderr)
            sys.exit(1)
        for pid in pids:
            hwnd = pick_largest_visible_hwnd(pid)
            if hwnd is not None:
                return hwnd, f"{exe} (pid={pid})"
        for pid in pids:
            hwnds = enumerate_hwnds_for_pid(pid)
            if hwnds:
                return hwnds[0], f"{exe} (pid={pid})"
        print(f"Process {exe!r} has no visible top-level windows.", file=sys.stderr)
        sys.exit(1)

    return None, ""


def _run_cv2(
    cam: Any,
    *,
    window_title: str,
    max_width: int,
    max_height: int,
    cv2: Any,
) -> None:
    # AUTOSIZE: client area matches image pixels 1:1 — avoids blur from stretching (WINDOW_NORMAL).
    cv2.namedWindow(window_title, cv2.WINDOW_AUTOSIZE)
    frame_times: list[float] = []
    frame_idx = 0
    poll = getattr(cv2, "pollKey", None)
    try:
        while True:
            t0 = time.perf_counter()
            frame = cam.get_latest_frame_view()
            if frame is None:
                continue
            frame = _preview_resize(frame, max_width, max_height, cv2)
            cv2.imshow(window_title, frame)
            t1 = time.perf_counter()
            frame_times.append(t1 - t0)
            if len(frame_times) > _FPS_ROLLING_SAMPLES:
                frame_times.pop(0)
            avg = sum(frame_times) / len(frame_times) if frame_times else 0.0
            fps = 1.0 / avg if avg > 0 else 0.0
            frame_idx += 1
            if frame_idx % _TITLE_FPS_EVERY_N_FRAMES == 0:
                cv2.setWindowTitle(window_title, f"{window_title} — {fps:.1f} FPS")

            if poll is not None:
                key = poll()
            else:
                key = cv2.waitKey(1)
            if key & 0xFF == ord("q") or key == ord("q"):
                break
            if frame_idx % 30 == 0:
                try:
                    if cv2.getWindowProperty(window_title, cv2.WND_PROP_VISIBLE) < 1:
                        break
                except cv2.error:
                    break
    finally:
        cv2.destroyAllWindows()


def main() -> None:
    args = _parse_args()
    target_hwnd, target_desc = _resolve_capture_hwnd(args)
    window_mode = target_hwnd is not None

    backend: CaptureBackend = cast(CaptureBackend, args.backend)
    if window_mode and backend != "winrt":
        backend = cast(CaptureBackend, "winrt")

    ocv = _analyze_opencv()
    if not ocv.gui_ok or ocv.module is None:
        print("OpenCV GUI is required for this example.\n", file=sys.stderr)
        if ocv.gui_diagnostic:
            print(ocv.gui_diagnostic, file=sys.stderr)
        sys.exit(1)

    processor_backend = "cv2" if ocv.processor_ok else "numpy"
    if not ocv.processor_ok:
        print("Using numpy color conversion (install opencv-python for best speed).\n", file=sys.stderr)

    kwargs: dict[str, Any] = {
        "backend": backend,
        "device_idx": args.device_idx,
        "output_color": "BGR",
        "processor_backend": processor_backend,
    }
    if args.output_idx is not None:
        kwargs["output_idx"] = args.output_idx
    if window_mode:
        kwargs["target_hwnd"] = target_hwnd

    camera = dxcam.create(**kwargs)
    window_title = (
        f"DXcam — {target_desc}" if window_mode else f"DXcam — monitor ({backend})"
    )

    if args.max_width <= 0 and args.max_height <= 0:
        preview_note = "preview native size"
    else:
        bits = []
        if args.max_width > 0:
            bits.append(f"max width {args.max_width}")
        if args.max_height > 0:
            bits.append(f"max height {args.max_height}")
        preview_note = "preview " + ", ".join(bits)
    print(
        f"Capture {camera.width}×{camera.height}  |  {preview_note}  |  "
        f"fps={args.target_fps or 'uncapped'}  |  q=quit"
        + (f"  |  {target_desc}" if window_mode else "")
    )

    cv2_mod = ocv.module
    try:
        camera.start(target_fps=args.target_fps, video_mode=args.video_mode)
        _run_cv2(
            camera,
            window_title=window_title,
            max_width=args.max_width,
            max_height=args.max_height,
            cv2=cv2_mod,
        )
    finally:
        camera.stop()
        camera.release()


if __name__ == "__main__":
    main()
