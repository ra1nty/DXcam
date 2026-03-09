# **DXcam**
> ***Fast Python Screen Capture for Windows - Updated 2026***

```python
import dxcam

camera = dxcam.create()
frame = camera.grab()
```

## Introduction
DXcam is a high-performance python screenshot and capture library for Windows based on the Desktop Duplication API.
It is designed for low-latency, high-FPS capture pipelines (including full-screen Direct3D applications).

Compared with common Python alternatives, DXcam focuses on:
- Higher capture throughput
- Stable capture for full-screen exclusive Direct3D apps
- Correct handling of scaled/stretched outputs
- Better FPS pacing for continuous/video capture

## Installation
### From PyPI (pip)
Recommended (with OpenCV):
```bash
pip install "dxcam[cv2]"
```

Enable WinRT backend support:
```bash
pip install "dxcam[winrt]"
```

Minimal install:
```bash
pip install dxcam
```

### From source (uv)
```bash
uv sync
# include OpenCV
uv sync --extra cv2
# include WinRT backend
uv sync --extra winrt
```

### Dev environment (uv + ruff + ty)
```bash
uv venv --python 3.11 .venv
uv sync --dev
uv run ruff check dxcam
uv run ty check dxcam
```

## Usage
Each output (monitor) is associated with one `DXCamera` instance.

```python
import dxcam
camera = dxcam.create()  # primary output on device 0
```

Backend selection:
```python
camera = dxcam.create(backend="dxgi")   # default Desktop Duplication path
camera = dxcam.create(backend="winrt")  # Windows.Graphics.Capture backend
```

### Screenshot
```python
frame = camera.grab()
```
`grab()` returns a `numpy.ndarray`. `None` if no new frame is available since the last capture, mainly for backward compatibility. You can use `new_frame_only=False` to change this behavior.

Use `copy=False` (or `camera.grab_view()`) for a zero-copy view.

To capture a region:
```python
left, top = (1920 - 640) // 2, (1080 - 640) // 2
right, bottom = left + 640, top + 640
frame = camera.grab(region=(left, top, right, bottom)) # numpy.ndarray of size (640x640x3) -> (HXWXC)
```

### Screen Capture
```python
camera.start(region=(left, top, right, bottom), target_fps=60)
camera.is_capturing  # True
# ...
camera.stop()
camera.is_capturing  # False
```

#### Consume the Screen Capture Data
```python
for _ in range(1000):
    frame = camera.get_latest_frame()  # blocks until a frame is available
```
>The screen capture mode spins up a thread polling the rendered new frames and store in an in-memory frame buffer. The blocking / video_mode behavior is designed with downstream video recording / machine learning applications in mind. 

Useful variants:
- `camera.get_latest_frame(with_timestamp=True)` -> `(frame, frame_timestamp)` -> return frame timestamp
- `camera.get_latest_frame_view()` -> zero-copy view into the frame buffer
- `camera.grab(copy=False)` / `camera.grab_view()` -> zero-copy latest-frame snapshot

** When `start()` capture is running, calling `grab()` reads from the in-memory ring buffer instead of directly polling DXGI.

## Advanced Usage and Remarks
### Multiple monitors / GPUs
```python
cam1 = dxcam.create(device_idx=0, output_idx=0)
cam2 = dxcam.create(device_idx=0, output_idx=1)
cam3 = dxcam.create(device_idx=1, output_idx=1)

img1 = cam1.grab()
img2 = cam2.grab()
img3 = cam3.grab()
```

Inspect available devices/outputs:
```pycon
>>> import dxcam
>>> print(dxcam.device_info())
'Device[0]:<Device Name:NVIDIA GeForce RTX 3090 Dedicated VRAM:24348Mb VendorId:4318>\n'
>>> print(dxcam.output_info())
'Device[0] Output[0]: Res:(1920, 1080) Rot:0 Primary:True\nDevice[0] Output[1]: Res:(1920, 1080) Rot:0 Primary:False\n'
```

### Output Format
Set output color mode when creating the camera:
```python
dxcam.create(output_color="BGRA")
```

Supported modes: `"RGB"`, `"RGBA"`, `"BGR"`, `"BGRA"`, `"GRAY"`.

Notes:
- Data is returned as `numpy.ndarray`.
- `BGRA` does not require OpenCV.
- Other color modes conversion require OpenCV (`dxcam[cv2]`).

### Frame Buffer
DXcam uses a fixed-size ring buffer in-memory. New frames overwrite old frames when full.

```python
camera = dxcam.create(max_buffer_len=120)  # default is 8
```

### Target FPS
DXcam uses high-resolution pacing with drift correction to run near `target_fps`.

```python
camera.start(target_fps=120)  # default to 60, greater than 120 is resource heavy
```

On Python 3.11+, DXcam relies on Windows high-resolution timer behavior used by `time.sleep()`.
On older versions, DXcam uses WinAPI waitable timers directly.

### Frame Timestamp
Read the most recent frame timestamp (seconds):
```python
camera.start(target_fps=60)
frame, ts = camera.get_latest_frame(with_timestamp=True)
camera.stop()
```

For `backend="dxgi"`, this value comes from `DXGI_OUTDUPL_FRAME_INFO.LastPresentTime`.
For `backend="winrt"`, this value is derived from WinRT `SystemRelativeTime`.

### Video Mode
With `video_mode=True`, DXcam fills the buffer at target FPS, reusing the previous frame if needed, even if no new frame is rendered.

```python
import cv2
import dxcam

target_fps = 30
camera = dxcam.create(output_color="BGR")
camera.start(target_fps=target_fps, video_mode=True)

writer = cv2.VideoWriter(
    "video.mp4", cv2.VideoWriter_fourcc(*"mp4v"), target_fps, (1920, 1080)
)
for _ in range(600):
    writer.write(camera.get_latest_frame())

camera.stop()
writer.release()
```

### Safely Releasing Resources
`release()` stops capture, frees buffers, and releases capture resources.
After `release()`, the same instance cannot be reused.

```python
camera = dxcam.create(output_idx=0, output_color="BGR")
camera.release()
# camera.start()  # raises RuntimeError
```

## Benchmarks
When using a similar logic (only capture newly rendered frames) running on a 240fps output, ```DXCam, python-mss, D3DShot``` benchmarked as follow:

|             | DXcam  | python-mss | D3DShot |
|-------------|--------|------------|---------|
| Average FPS | 239.19 :checkered_flag: | 75.87      | 118.36  |
| Std Dev     | 1.25   | 0.5447     | 0.3224   |

The benchmark is across 5 runs, with a light-moderate usage on my PC (5900X + 3090; Chrome ~30tabs, VS Code opened, etc.), I used the [Blur Buster UFO test](https://www.testufo.com/framerates#count=5&background=stars&pps=960) to constantly render 240 fps on my monitor. DXcam captured almost every frame rendered. You will see some benchmarks online claiming 1000+fps capture while most of them is busy-spinning a for loop on a staled frame (no new frame rendered on screen in test scenario).

### For Targeting FPS:
|   (Target)\\(mean,std)          | DXcam  | python-mss | D3DShot |
|-------------  |--------                 |------------|---------|
| 60fps         | 61.71, 0.26 :checkered_flag: | N/A     | 47.11, 1.33  |
| 30fps         | 30.08, 0.02 :checkered_flag:  | N/A     | 21.24, 0.17  |


## Work Referenced

[OBS Studio](https://github.com/obsproject/obs-studio) - implementation ideas and references.

[D3DShot](https://github.com/SerpentAI/D3DShot/) : DXcam borrows the ctypes header directly from the no-longer maintained D3DShot.
