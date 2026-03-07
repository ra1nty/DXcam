import logging
import time

import mss


TOP = 0
LEFT = 0
RIGHT = 1920
BOTTOM = 1080
region = (LEFT, TOP, RIGHT, BOTTOM)
title = "[MSS] FPS benchmark"
TARGET_FRAMES = 1000

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

fps = 0
sct = mss.mss()
start_time = time.perf_counter()
logger.info("Starting %s. region=%s target_frames=%d", title, region, TARGET_FRAMES)

while fps < TARGET_FRAMES:
    frame = sct.grab(region)
    if frame is not None:
        fps += 1
        if fps % 250 == 0:
            logger.debug("Captured %d/%d frames", fps, TARGET_FRAMES)


elapsed_s = time.perf_counter() - start_time
logger.info("%s result: %.3f fps", title, fps / elapsed_s)
