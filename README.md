# **DXcam**: Fastest Python Screenshot Library for Windows
DXcam is a Python high-performance screenshot library for Windows using Desktop Duplication API. Capable of 240Hz desktop capturing, makes it suitable for FPS game capturing (CS:GO, Valorant, etc.). 

## Introduction
It was originally built as a part of deep learning pipeline for FPS games to perform better than existed python solutions ([python-mss](https://github.com/BoboTiG/python-mss), [D3DShot](https://github.com/SerpentAI/D3DShot/)). 

Compared to these existed solutions, DXcam provides:
- Way faster screen capturing speed (> 240Hz)
- Capturing of Direct3D exclusive full-screen application without interrupting, even when alt+tab.
- Automatic handling of scaled / stretched resolution.
- Accurate FPS targeting when in capturing mode. 
- Seamless integration with NumPy, OpenCV, PyTorch, etc.

## **In construction: Everything here is messy and experimental. Features are still incomplete. Use with caution.**

## Installation
Only from source available before pre-release.

```pip install```

## Usage
In DXCam, each output (monitor) is asscociated to a ```DXCamera``` instance.
To create a DXCamera instance:
```python
import dxcam
camera = dxcam.create()  # returns a DXCamera instance on primary monitor
```
### Screenshot
For screenshot, simply use ```.grab```:
```python
frame = camera.grab()
```
The returned ```frame``` will be a ```numpy.ndarray``` in the shape of ```(Height,  Width, 3[RGB])```. This is the default and the only supported format (**for now**). It is worth noting that ```.grab``` will return ```None``` if there is no new frame since the last time you called ```.grab```. Usually it means there's nothing new to render since last time (E.g. You are idling).

To view the captured screenshot:
```python
from PIL import Image
Image.fromarray(frame).show()
```
To screenshot a specific region, use the ```region``` parameter: it takes ```tuple[int, int, int, int]``` as the left, top, right, bottom coordinates of the bounding box. Similar to [PIL.ImageGrab.grab](https://pillow.readthedocs.io/en/stable/reference/ImageGrab.html).
```python
left, top = (1920 - 640) // 2, (1080 - 640) // 2
right, bottom = left + 640, top + 640
region = (left, top, right, bottom)
frame = camera.grab(region=region)
```
The above code will take a screenshot of the center ```640x640``` portion of a ```1920x1080``` monitor.
### Screen Capture
To start a screen capture, simply use ```.start```: the capture will be started in a separated thread, default at 60Hz. Use ```.stop``` to stop the capture.
```python
camera.start()
camera.is_capturing  # True
# ... Do Something
camera.stop()
camera.is_capturing  # False
```

While the ```DXCamera``` instance is in capture mode, you can use ```.get_latest_frame``` to get the latest (LIFO) frame in the frame buffer:
```python
camera.start(target_fps=60)
for i in range(1000):
    image = camera.get_latest_frame()  # Will block until new frame available
camera.stop()
```
Notice that ```.get_latest_frame``` will block until there is a new frame available since the last call to ```.get_latest_frame```.

## Advanced Usage and Remarks
### For multiple monitors:
```python
cam1 = dxcam.create(device_idx=0, output_idx=0)
cam2 = dxcam.create(device_idx=0, output_idx=1)
cam3 = dxcam.create(device_idx=1, output_idx=1)
img1 = cam1.grab()
img2 = cam2.grab()
img2 = cam3.grab()
```
The above code creates three ```DXCamera``` instances for:
- monitor 0 on device (GPU) 0, monitor 1 on device (GPU) 0, monitor 1 on device (GPU) 1

and subsequently takes three full-screen screenshots. (cross GPU untested, but I hope it works.)

## Benchmark
### For Max FPS Capability:
```python
import time
import dxcam

title = "[DXcam] FPS benchmark"
start_time, fps = time.perf_counter(), 0
cam = dxcam.create()
start = time.perf_counter()
while fps < 1000:
    frame = cam.grab()
    if frame is not None:  # New frame
        fps += 1
end_time = time.perf_counter() - start_time

print(f"{title}: {fps/end_time}")
del cam
```

## Work Referenced:
[D3DShot](https://github.com/SerpentAI/D3DShot/) : DXcam borrows the ctypes header directly from the no-longer maintained D3DShot.

[OBS Studio](https://github.com/obsproject/obs-studio) : Learned a lot from it.