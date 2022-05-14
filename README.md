# **DXcam**
> ***Fastest Python Screenshot for Windows***
```python
import dxcam
camera = dxcam.create()
camera.grab()
```

## Introduction
DXcam is a Python high-performance screenshot library for Windows using Desktop Duplication API. Capable of 240Hz+ capturing. It was originally built as a part of deep learning pipeline for FPS games to perform better than existed python solutions ([python-mss](https://github.com/BoboTiG/python-mss), [D3DShot](https://github.com/SerpentAI/D3DShot/)). 

Compared to these existed solutions, DXcam provides:
- Way faster screen capturing speed (> 240Hz)
- Capturing of Direct3D exclusive full-screen application without interrupting, even when alt+tab.
- Automatic handling of scaled / stretched resolution.
- Accurate FPS targeting when in capturing mode, makes it suitable for Video output. 
- Seamless integration with NumPy, OpenCV, PyTorch, etc.

> ***In construction: Everything here is messy and experimental. Features are still incomplete. Use with caution.***

> ***Contributions are welcome!***

## Installation
### From TestPyPI:
```bash
pip install -i https://test.pypi.org/simple/ dxcam
```

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
frame = camera.grab(region=region)  # numpy.ndarray of size (640x640x3) -> (HXWXC)
```
The above code will take a screenshot of the center ```640x640``` portion of a ```1920x1080``` monitor.
### Screen Capture
To start a screen capture, simply use ```.start```: the capture will be started in a separated thread, default at 60Hz. Use ```.stop``` to stop the capture.
```python
camera.start(region=(left, top, right, bottom))  # Optional argument to capture a region
camera.is_capturing  # True
# ... Do Something
camera.stop()
camera.is_capturing  # False
```
### Consume the Screen Capture Data
While the ```DXCamera``` instance is in capture mode, you can use ```.get_latest_frame``` to get the latest frame in the frame buffer:
```python
camera.start()
for i in range(1000):
    image = camera.get_latest_frame()  # Will block until new frame available
camera.stop()
```
Notice that ```.get_latest_frame``` by default will block until there is a new frame available since the last call to ```.get_latest_frame```. To change this behavior, use ```video_mode=True```.

## Advanced Usage and Remarks
### Multiple monitors / GPUs
```python
cam1 = dxcam.create(device_idx=0, output_idx=0)
cam2 = dxcam.create(device_idx=0, output_idx=1)
cam3 = dxcam.create(device_idx=1, output_idx=1)
img1 = cam1.grab()
img2 = cam2.grab()
img2 = cam3.grab()
```
The above code creates three ```DXCamera``` instances for: ```[monitor0, GPU0], [monitor1, GPU0], [monitor1, GPU1]```, and subsequently takes three full-screen screenshots. (cross GPU untested, but I hope it works.) To get a complete list of devices and outputs:
```pycon
>>> import dxcam
>>> dxcam.device_info()
'Device[0]:<Device Name:NVIDIA GeForce RTX 3090 Dedicated VRAM:24348Mb VendorId:4318>\n'
>>> dxcam.output_info()
'Device[0] Output[0]: Res:(1920, 1080) Rot:0 Primary:True\nDevice[0] Output[1]: Res:(1920, 1080) Rot:0 Primary:False\n'
```

### Output Format
You can specify the output color mode upon creation of the DXCamera instance:
```python
dxcam.create(output_idx=0, output_color="BGRA")
```
We currently support "RGB", "RGBA", "BGR", "BGRA", "GRAY", with "GRAY being the gray scale. As for the data format, ```DXCamera``` only supports ```numpy.ndarray```  in shape of ```(Height, Width, Channels)``` right now. ***We will soon add support for other output formats.***

### Video Buffer
The captured frames will be insert into a fixed-size ring buffer, and when the buffer is full the newest frame will replace the oldest frame. You can specify the max buffer length (defualt to 64) using the argument ```max_buffer_len``` upon creation of the ```DXCamera``` instance. 
```python
camera = dxcam.create(max_buffer_len=512)
```
***Note:  Right now to consume frames during capturing there is only `get_latest_frame` available which assume the user to process frames in a LIFO pattern. This is a read-only action and won't pop the processed frame from the buffer. we will make changes to support various of consuming pattern soon.***

### Target FPS
To make ```DXCamera``` capture close to the user specified ```target_fps```, we used the undocumented ```CREATE_WAITABLE_TIMER_HIGH_RESOLUTION ``` flag to create a Windows [Waitable Timer Object](https://docs.microsoft.com/en-us/windows/win32/sync/waitable-timer-objects). This is far more accurate (+/- 1ms) than Python (<3.11) ```time.sleep``` (min resolution 16ms). The implementation is done through ```ctypes``` creating a perodic timer. Python 3.11 used a similar approach[^2]. 
```python
camera.start(target_fps=120)  # Should not be made greater than 160.
```
However, due to Windows itself is a preemptive OS[^1] and the overhead of Python calls, the target FPS can not be guarenteed accurate when greater than 160. (See Benchmarks)


### Video Mode
The default behavior of ```.get_latest_frame``` only put newly rendered frame in the buffer, which suits the usage scenario of a object detection/machine learning pipeline. However, when recording a video that is not ideal since we aim to get the frames at a constant framerate: When the ```video_mode=True``` is specified when calling ```.start``` method of a ```DXCamera``` instance, the frame buffer will be feeded at the target fps, using the last frame if there is no new frame available. For example, the following code output a 5-second, 120Hz screen capture:
```python
target_fps = 120
camera = dxcam.create(output_idx=0, output_color="BGR")
camera.start(target_fps=target_fps, video_mode=True)
writer = cv2.VideoWriter(
    "video.mp4", cv2.VideoWriter_fourcc(*"mp4v"), target_fps, (1920, 1080)
)
for i in range(600):
    writer.write(camera.get_latest_frame())
camera.stop()
writer.release()
```

### Safely Releasing of Resource
Upon calling ```.release``` on a DXCamera instance, it will stop any active capturing, free the buffer and release the duplicator and staging resource. Upon calling ```.stop()```, DXCamera will stop the active capture and free the frame buffer. If you want to manually recreate a ```DXCamera``` instance on the same output with different parameters, you can also manully delete it:
```python
camera1 = dxcam.create(output_idx=0, output_color="BGR")
camera2 = dxcam.create(output_idx=0)  # Not allowed, camera1 will be returned
camera1 is camera2  # True
del camera1
del camera2
camera2 = dxcam.create(output_idx=0)  # Allowed
```

## Benchmarks
### For Max FPS Capability:
```python
start_time, fps = time.perf_counter(), 0
cam = dxcam.create()
start = time.perf_counter()
while fps < 1000:
    frame = cam.grab()
    if frame is not None:  # New frame
        fps += 1
end_time = time.perf_counter() - start_time
print(f"{title}: {fps/end_time}")
```
When using a similar logistic (only captured new frame counts), ```DXCam, python-mss, D3DShot``` benchmarked as follow:

|             | DXcam  | python-mss | D3DShot |
|-------------|--------|------------|---------|
| Average FPS | 238.79 :checkered_flag: | 75.87      | 118.36  |
| Std Dev     | 1.25   | 0.5447     | 0.3224   |

The benchmark is across 5 runs, with a light-moderate usage on my PC (5900X + 3090; Chrome ~30tabs, VS Code opened, etc.), I used the [Blur Buster UFO test](https://www.testufo.com/framerates#count=5&background=stars&pps=960) to constantly render 240 fps on my monitor (Zowie 2546K). DXcam captured almost every frame rendered.

### For Targeting FPS:
```python
camera = dxcam.create(output_idx=0)
camera.start(target_fps=60)
for i in range(1000):
    image = camera.get_latest_frame()
camera.stop()
```
|   (Target)\\(mean,std)          | DXcam  | python-mss | D3DShot |
|-------------  |--------                 |------------|---------|
| 60fps         | 61.71, 0.26 :checkered_flag: | N/A     | 47.11, 1.33  |
| 30fps         | 30.08, 0.02 :checkered_flag:  | N/A     | 21.24, 0.17  |

## Work Referenced
[D3DShot](https://github.com/SerpentAI/D3DShot/) : DXcam borrows the ctypes header directly from the no-longer maintained D3DShot.

[OBS Studio](https://github.com/obsproject/obs-studio) : Learned a lot from it.


[^1]: <https://en.wikipedia.org/wiki/Preemption_(computing)> Preemption (computing)

[^2]: <https://github.com/python/cpython/issues/65501> bpo-21302: time.sleep() uses waitable timer on Windows
