import dxcam
import cv2

TOP = 0
LEFT = 0
RIGHT = 1920
BOTTOM = 1080
region = (LEFT, TOP, RIGHT, BOTTOM)
title = "[DXcam] Capture benchmark"

target_fps = 120
camera = dxcam.create(output_idx=0)
camera.start(target_fps=target_fps, video_mode=False)
writer = cv2.VideoWriter(
    "video.mp4", cv2.VideoWriter_fourcc(*"mp4v"), target_fps, (1920, 1080)
)
for i in range(600):
    writer.write(cv2.cvtColor(camera.get_latest_frame(), cv2.COLOR_RGB2BGR))
camera.stop()
del camera
writer.release()
