import argparse
import logging
import time

import dxcam


TOP = 0
LEFT = 0
RIGHT = 3840
BOTTOM = 2160
REGION = (LEFT, TOP, RIGHT, BOTTOM)
TITLE = "[DXcam] Capture benchmark"
TARGET_FRAMES = 1000
TARGET_FPS = 60

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run DXcam capture benchmark.")
    parser.add_argument(
        "--backend",
        choices=("dxgi", "winrt"),
        default="dxgi",
        help="Capture backend to benchmark (default: dxgi).",
    )
    parser.add_argument(
        "--target-fps",
        type=int,
        default=TARGET_FPS,
        help=f"Capture target FPS (default: {TARGET_FPS}).",
    )
    parser.add_argument(
        "--target-frames",
        type=int,
        default=TARGET_FRAMES,
        help=f"Number of benchmark reads (default: {TARGET_FRAMES}).",
    )
    parser.add_argument(
        "--processor-backend",
        choices=("cv2", "cython", "numpy"),
        default="cv2",
        help="Post-processing backend (default: cv2).",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    camera = dxcam.create(
        output_idx=0,
        backend=args.backend,
        processor_backend=args.processor_backend,
    )
    camera.start(target_fps=args.target_fps, video_mode=False)
    logger.info(
        "Starting %s. backend=%s processor_backend=%s region=%s target_fps=%d target_frames=%d",
        TITLE,
        args.backend,
        args.processor_backend,
        REGION,
        args.target_fps,
        args.target_frames,
    )
    start_time = time.perf_counter()
    captured = 0
    duplicate_timestamps = 0
    estimated_skipped_frames = 0
    last_ts = None
    target_period = (1.0 / args.target_fps) if args.target_fps > 0 else None
    for idx in range(args.target_frames):
        result = camera.get_latest_frame(with_timestamp=True)
        if result is not None:
            _image, ts = result
            captured += 1
            if last_ts is not None:
                delta = ts - last_ts
                if delta <= 0:
                    duplicate_timestamps += 1
                elif target_period is not None and delta > (target_period * 1.5):
                    estimated_skipped_frames += max(
                        0, round(delta / target_period) - 1
                    )
            last_ts = ts
        if (idx + 1) % 250 == 0:
            logger.debug(
                "Read frames=%d/%d non_none=%d dup_ts=%d est_skipped=%d",
                idx + 1,
                args.target_frames,
                captured,
                duplicate_timestamps,
                estimated_skipped_frames,
            )
    camera.stop()
    elapsed_s = time.perf_counter() - start_time
    logger.info(
        "done elapsed=%.3fs non_none=%d dup_ts=%d est_skipped=%d",
        elapsed_s,
        captured,
        duplicate_timestamps,
        estimated_skipped_frames,
    )
    camera.release()


if __name__ == "__main__":
    main()
