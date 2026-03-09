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
Minimal install:
```bash
pip install dxcam
```

Full feature: (includes OpenCV-based color conversion, WinRT capture backend support:):
```bash
pip install "dxcam[cv2,winrt]"
```

Notes:
- Official Windows wheels are built for CPython `3.10` to `3.14`.
- Binary wheels include the Cython kernels used by processor backends.

### From source (uv)
```bash
uv sync
# include OpenCV conversion backend
uv sync --extra cv2
# include optional Cython tooling
uv sync --extra cython
# include WinRT backend
uv sync --extra winrt
```

Build local Cython kernels from source:
```bash
set DXCAM_BUILD_CYTHON=1
uv pip install -e .[cython] --no-build-isolation
```

### Dev environment (uv + ruff + ty)
```bash
uv venv --python 3.11 .venv
uv sync --dev
uv run ruff check dxcam
uv run ty check dxcam
```

### API docs (pdoc)
Generate autodocs for the public API surface:
```bash
uv run pdoc -d google -o site dxcam dxcam.dxcam dxcam.types
```

Preview locally by opening `site/index.html`.
CI builds docs on pull requests, and docs are deployed from `main` via GitHub Pages.

## Usage
Each output (monitor) is associated with one `DXCamera` instance.

```python
import dxcam
camera = dxcam.create()  # primary output on device 0
```

To specify backend:
```python
camera = dxcam.create(
    backend="dxgi", # default Desktop Duplication backend
    processor_backend="cv2" # default OpenCV processor
)
```

### Screenshot
```python
frame = camera.grab()
```
`grab()` returns a `numpy.ndarray`. `None` if no new frame is available since the last capture (for backward compatibility); use `new_frame_only=False` to reuse the latest cached one-shot frame.

Use `copy=False` (or `camera.grab_view()`) for a zero-copy view. This is faster, but the returned buffer can be overwritten by later captures.

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
>The screen capture mode spins up a thread that polls newly rendered frames and stores them in an in-memory ring buffer. The blocking and `video_mode` behavior is designed for downstream video recording and machine learning workloads.

Useful variants:
- `camera.get_latest_frame(with_timestamp=True)` -> `(frame, frame_timestamp)` -> return frame timestamp
- `camera.get_latest_frame_view()` -> zero-copy view into the frame buffer
- `camera.grab(copy=False)` / `camera.grab_view()` -> zero-copy latest-frame snapshot

> When `start()` capture is running, calling `grab()` reads from the in-memory ring buffer instead of directly polling DXGI.

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
- `BGRA` does not require OpenCV and is the leanest dependency path.
- `RGB`, `BGR`, `RGBA`, `GRAY` require conversion (`cv2` or compiled `numpy` backend).

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

### Capture Backend
DXcam supports two capture backends:
- `dxgi` (default): Desktop Duplication API path with broad compatibility.
- `winrt`: Windows Graphics Capture path.

Use it like this:
```python
camera = dxcam.create(backend="dxgi")   # default
camera = dxcam.create(backend="winrt")
```

Guideline:
- Start with `dxgi` for most workloads.
- Try `winrt` if it performs better on your machine or fits your app constraints.

### Processor Backend
DXcam capture backends (`dxgi`/`winrt`) first acquire a BGRA frame.  
The processor backend then handles post-processing:
- optional rotation/cropping preparation
- color conversion to your `output_color`

Recommended backend choice:
- OpenCV installed: use `cv2` (default)
- No OpenCV installed: use `numpy` (Cython kernels)

Use it like this:
```python
camera = dxcam.create(processor_backend="cv2")
camera = dxcam.create(processor_backend="numpy")
```

Official Windows wheels already include the compiled NumPy kernels.

Only for source installs:
```bash
set DXCAM_BUILD_CYTHON=1
pip install -e .[cython] --no-build-isolation
```

If `processor_backend="numpy"` is selected but compiled kernels are unavailable,
DXcam logs a warning and falls back to `cv2` behavior. In that fallback path,
install OpenCV for non-`BGRA` output modes.

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

Processor backend comparison helper:
```bash
python benchmarks/dxcam_processor_compare.py --backend dxgi --target-fps 120 --target-frames 1000
python benchmarks/dxcam_capture.py --backend dxgi --processor-backend numpy
python benchmarks/numpy_processor_micro.py --width 3840 --height 2160 --modes RGB --variants process into --processor-backends cv2 numpy
```


## Work Referenced

[OBS Studio](https://github.com/obsproject/obs-studio) - implementation ideas and references.

[D3DShot](https://github.com/SerpentAI/D3DShot/) : DXcam borrows the ctypes header directly from the no-longer maintained D3DShot.
