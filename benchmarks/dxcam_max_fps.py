import argparse
import logging
import sys
import time

import dxcam


TOP = 0
LEFT = 0
RIGHT = 1920
BOTTOM = 1080
REGION = (LEFT, TOP, RIGHT, BOTTOM)
TITLE = "[DXcam] FPS benchmark"
TARGET_FRAMES = 1000
NEW_FRAME_ONLY = False

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run DXcam max FPS benchmark.")
    parser.add_argument(
        "--backend",
        choices=("dxgi", "winrt"),
        default="dxgi",
        help="Capture backend to benchmark (default: dxgi).",
    )
    parser.add_argument(
        "--target-frames",
        type=int,
        default=TARGET_FRAMES,
        help=f"Number of frames to capture (default: {TARGET_FRAMES}).",
    )
    parser.add_argument(
        "--processor-backend",
        choices=("cv2", "numpy"),
        default="cv2",
        help="Post-processing backend (default: cv2).",
    )
    parser.add_argument(
        "--pid",
        type=int,
        default=None,
        help="Process ID for window capture (WinRT only). Requires --backend=winrt.",
    )
    parser.add_argument(
        "--process",
        type=str,
        default=None,
        help="Process name (e.g. notepad.exe) for window capture. Finds first matching PID.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    fps = 0

    pid = args.pid
    if pid is None and args.process is not None:
        try:
            import psutil
        except ImportError:
            logger.error("--process requires psutil: pip install psutil")
            sys.exit(1)
        for proc in psutil.process_iter(["name", "pid"]):
            try:
                if proc.info["name"] and args.process.lower() in proc.info["name"].lower():
                    pid = proc.info["pid"]
                    logger.info("Resolved --process %s -> PID %d", args.process, pid)
                    break
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        if pid is None:
            logger.error("No process found matching %r", args.process)
            sys.exit(1)

    if pid is not None:
        if args.backend != "winrt":
            logger.error("--pid/--process requires --backend=winrt")
            sys.exit(1)
        from dxcam.util.hwnd import pick_largest_visible_hwnd

        hwnd = pick_largest_visible_hwnd(pid)
        if hwnd is None:
            logger.error("No visible window found for PID %d", args.pid)
            sys.exit(1)
        cam = dxcam.create(
            backend="winrt",
            processor_backend=args.processor_backend,
            output_idx=0,
            output_color="BGRA",
            target_hwnd=hwnd,
        )
        logger.info("Window capture: PID=%d hwnd=%d size=%dx%d", pid, hwnd, cam.width, cam.height)
        grab_kw = {"new_frame_only": NEW_FRAME_ONLY}
    else:
        cam = dxcam.create(
            backend=args.backend,
            processor_backend=args.processor_backend,
            output_idx=0,
            output_color="BGRA",
        )
        grab_kw = {"region": REGION, "new_frame_only": NEW_FRAME_ONLY}

    start_time = time.perf_counter()
    logger.info(
        "Starting %s. backend=%s processor_backend=%s target_frames=%d",
        TITLE,
        args.backend,
        args.processor_backend,
        args.target_frames,
    )

    while fps < args.target_frames:
        frame = cam.grab(**grab_kw)
        if frame is not None:
            fps += 1
            if fps % 250 == 0:
                logger.debug("Captured %d/%d frames", fps, args.target_frames)

    elapsed_s = time.perf_counter() - start_time
    logger.info(
        "%s result (%s/%s): %.3f fps",
        TITLE,
        args.backend,
        args.processor_backend,
        fps / elapsed_s,
    )
    cam.release()


if __name__ == "__main__":
    main()
