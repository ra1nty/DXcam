import argparse
import logging
import time

import dxcam


TOP = 0
LEFT = 0
RIGHT = 3840
BOTTOM = 2160
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
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    fps = 0
    cam = dxcam.create(backend=args.backend, processor_backend=args.processor_backend, output_idx=0)
    start_time = time.perf_counter()
    logger.info(
        "Starting %s. backend=%s processor_backend=%s region=%s target_frames=%d",
        TITLE,
        args.backend,
        args.processor_backend,
        REGION,
        args.target_frames,
    )

    while fps < args.target_frames:
        frame = cam.grab(region=REGION, new_frame_only=NEW_FRAME_ONLY)
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
