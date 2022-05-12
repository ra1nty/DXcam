import time
import dxcam


TOP = 0
LEFT = 0
RIGHT = 1920
BOTTOM = 1080
region = (LEFT, TOP, RIGHT, BOTTOM)
title = "[DXcam] Capture benchmark"

fps = 0
camera = dxcam.create(output_idx=0)
camera.start(target_fps=60)
for i in range(1000):
    image = camera.get_latest_frame()
camera.stop()
del camera
