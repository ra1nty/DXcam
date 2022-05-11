import time
import mss


TOP = 0
LEFT = 0
RIGHT = 1920
BOTTOM = 1080
region = (LEFT, TOP, RIGHT, BOTTOM)
title = "[MSS] FPS benchmark"
start_time = time.perf_counter()


fps = 0
sct = mss.mss()
start = time.perf_counter()
while fps < 1000:
    frame = sct.grab(region)
    if frame is not None:
        fps += 1


end_time = time.perf_counter() - start_time

print(f"{title}: {fps/end_time}")
