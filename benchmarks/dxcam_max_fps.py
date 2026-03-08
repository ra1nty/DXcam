import logging
import time

import dxcam


TOP = 0
LEFT = 0
RIGHT = 1920
BOTTOM = 1080
region = (LEFT, TOP, RIGHT, BOTTOM)
title = "[DXcam] FPS benchmark"
TARGET_FRAMES = 1000
NEW_FRAME_ONLY = False

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

fps = 0
cam = dxcam.create()
start_time = time.perf_counter()
logger.info("Starting %s. region=%s target_frames=%d", title, region, TARGET_FRAMES)

while fps < TARGET_FRAMES:
    frame = cam.grab(region=region, new_frame_only=NEW_FRAME_ONLY)
    if frame is not None:
        fps += 1
        if fps % 250 == 0:
            logger.debug("Captured %d/%d frames", fps, TARGET_FRAMES)

elapsed_s = time.perf_counter() - start_time
logger.info("%s result: %.3f fps", title, fps / elapsed_s)
del cam
