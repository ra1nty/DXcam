import time
import d3dshot


TOP = 0
LEFT = 0
RIGHT = 1920
BOTTOM = 1080
region = (LEFT, TOP, RIGHT, BOTTOM)
title = "[D3DShot] FPS benchmark"
start_time = time.perf_counter()

fps = 0


sct = d3dshot.create(capture_output="numpy")


start = time.perf_counter()
while fps < 1000:
    frame = sct.screenshot(region)
    if frame is not None:
        fps += 1


end_time = time.perf_counter() - start_time

print(f"{title}: {fps/end_time}")
sct.stop()
