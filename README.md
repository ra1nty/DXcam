# DXcam
## Introduction
DXcam is a Python high-performance screenshot library for Windows using Desktop Duplication API. Capable of 240Hz desktop capturing, makes it suitable for FPS game capturing (CS:GO, Valorant, etc.). It was originally built as a part of deep learning pipeline for FPS games to perform better than existed python solutions ([python-mss](https://github.com/BoboTiG/python-mss), [D3DShot](https://github.com/SerpentAI/D3DShot/)).

This library borrows the ctypes header directly from the no-longer maintained **[D3DShot](https://github.com/SerpentAI/D3DShot/)**.

## **In construction: Everything here is messy and experimental. Features are still incomplete.**



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
The returned ```frame``` will be a ```numpy.ndarray``` in the shape of ```(Height,  Width, 3[RGB])```. This is the default and the only supported format (**for now**).
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
    if frame is not None:
        fps += 1
end_time = time.perf_counter() - start_time

print(f"{title}: {fps/end_time}")
del cam
```

## Work Referenced:
[D3DShot](https://github.com/SerpentAI/D3DShot/)

[OBS Studio](https://github.com/obsproject/obs-studio)