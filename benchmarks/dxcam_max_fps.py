import time
import dxcam


TOP = 0
LEFT = 0
RIGHT = 1920
BOTTOM = 1080
region = (LEFT, TOP, RIGHT, BOTTOM)
title = "[DXcam] FPS benchmark"
start_time = time.perf_counter()


fps = 0
cam = dxcam.create()
start = time.perf_counter()
while fps < 1000:
    frame = cam.grab(region=region)
    if frame is not None:
        fps += 1

end_time = time.perf_counter() - start_time

print(f"{title}: {fps/end_time}")
del cam
