import logging
import time

import dxcam


TOP = 0
LEFT = 0
RIGHT = 3840
BOTTOM = 2160
region = (LEFT, TOP, RIGHT, BOTTOM)
title = "[DXcam] Capture benchmark"
TARGET_FRAMES = 1000
TARGET_FPS = 60

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

camera = dxcam.create(output_idx=0)
camera.start(target_fps=TARGET_FPS, video_mode=False)
logger.info(
    "Starting %s. region=%s target_fps=%d target_frames=%d",
    title,
    region,
    TARGET_FPS,
    TARGET_FRAMES,
)
start_time = time.perf_counter()
captured = 0
duplicate_timestamps = 0
estimated_skipped_frames = 0
last_ts = None
target_period = 1.0 / TARGET_FPS
for idx in range(TARGET_FRAMES):
    result = camera.get_latest_frame(with_timestamp=True)
    if result is not None:
        image, ts = result
        captured += 1
        if last_ts is not None:
            delta = ts - last_ts
            if delta <= 0:
                duplicate_timestamps += 1
            elif delta > (target_period * 1.5):
                estimated_skipped_frames += max(0, round(delta / target_period) - 1)
        last_ts = ts
    if (idx + 1) % 250 == 0:
        logger.debug(
            "Read frames=%d/%d non_none=%d dup_ts=%d est_skipped=%d",
            idx + 1,
            TARGET_FRAMES,
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
del camera
