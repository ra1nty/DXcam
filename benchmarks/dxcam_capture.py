import logging
import time

import dxcam


TOP = 0
LEFT = 0
RIGHT = 1920
BOTTOM = 1080
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
camera.start(target_fps=TARGET_FPS)
logger.info(
    "Starting %s. region=%s target_fps=%d target_frames=%d",
    title,
    region,
    TARGET_FPS,
    TARGET_FRAMES,
)
start_time = time.perf_counter()
captured = 0
for idx in range(TARGET_FRAMES):
    image = camera.get_latest_frame()
    if image is not None:
        captured += 1
    if (idx + 1) % 250 == 0:
        logger.debug(
            "Read frames=%d/%d non_none=%d",
            idx + 1,
            TARGET_FRAMES,
            captured,
        )
camera.stop()
elapsed_s = time.perf_counter() - start_time
logger.info(
    "%s result: %.3f read-fps non_none=%d",
    title,
    TARGET_FRAMES / elapsed_s,
    captured,
)
del camera
