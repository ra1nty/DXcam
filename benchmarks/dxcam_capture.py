import time
import dxcam


TOP = 0
LEFT = 0
RIGHT = 1920
BOTTOM = 1080
region = (LEFT, TOP, RIGHT, BOTTOM)
title = "[DXcam] Capture benchmark"

fps = 0
camera = dxcam.create()
camera.start(target_fps=60)
time.sleep(10)
camera.stop()
del camera
