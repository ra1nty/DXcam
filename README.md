# **DXcam**
> ***Fast Python Screen Capture for Windows - Updated 2026***

```python
import dxcam

with dxcam.create() as camera:
    frame = camera.grab()
```

> **Live API Docs:** [https://ra1nty.github.io/DXcam/](https://ra1nty.github.io/DXcam/)

## Introduction
DXcam is a high-performance python screenshot and capture library for Windows based on the Desktop Duplication API.
It is designed for low-latency, high-FPS capture pipelines (including full-screen Direct3D applications).

Compared with common Python alternatives, DXcam focuses on:
- Higher capture throughput (240+fps on 1080p)
- Stable capture for full-screen exclusive Direct3D apps
- Better FPS pacing for continuous video capture
- Support DXGI / Windows Graphics Capture dual backend
- Seamless integration for AI Agent / Computer Vision use cases.

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

### From source
Please refer to [CONTRIBUTING](CONTRIBUTING.md).

### Contributing / Dev
Contributions are welcome!
Development setup and contributor workflow are documented in [CONTRIBUTING.md](CONTRIBUTING.md).

## Usage
Each output (monitor) is associated with one `DXCamera` instance.

```python
import dxcam
camera = dxcam.create()  # primary output on device 0
```

To specify backends:
```python
camera = dxcam.create(
    backend="dxgi", # default Desktop Duplication backend
    processor_backend="cv2" # default OpenCV processor
)
```
Note:
- Version 0.4 uses a fixed three-slot frame buffer; `max_buffer_len` has been removed.
- Device discovery happens on the first `create()`, `device_info()`, or `output_info()` call.
- Upgrading from 0.3? See the [0.4 migration guide](https://github.com/ra1nty/DXcam/blob/dev/docs/migration-0.4.md).

### Screenshot
```python
frame = camera.grab()
```
`grab()` returns a `numpy.ndarray`. In one-shot mode it returns `None` if no new frame is available; `camera.grab(new_frame_only=False)` can reuse the last cached frame. During threaded capture, it reads the latest published frame and ignores `new_frame_only`.

Use `camera.grab_into(dst)` to reuse caller-managed memory.
Both `grab_into(dst)` and `get_latest_frame_into(dst)` require a writable NumPy
`uint8` array with the frame's `(height, width, channels)` shape. The unreleased
development branch raises `ValueError` for a read-only destination before writing.

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
    frame = camera.get_latest_frame()  # waits for the first available frame
```
The capture thread publishes into a latest-only frame buffer. Once a frame is available, reads return immediately and can return the same timestamp repeatedly. Consumers control their own pacing. `target_fps` controls the producer, not the frequency of consumer reads.

Useful variants:
- `camera.get_latest_frame(with_timestamp=True)` -> `(frame, frame_timestamp)` -> return frame timestamp
- `camera.get_latest_frame_into(dst)` -> write latest frame into caller-provided array

#### Wait for a Fresh Frame (Unreleased)
The current development branch adds `after_timestamp` and `timeout` to both latest-frame methods. These options are not included in `0.4.0.dev2`.

```python
last_timestamp = None
for _ in range(1000):
    result = camera.get_latest_frame(
        with_timestamp=True, after_timestamp=last_timestamp, timeout=0.5
    )
    if result is None:
        break  # no eligible frame before timeout, or capture stopped/failed
    frame, last_timestamp = result
    # Process frame here.
```

`after_timestamp` requires a strictly newer source timestamp. Each consumer keeps its own threshold; one reader does not consume another reader's update. A slow consumer receives the newest available frame and can skip intermediate frames. Freshness follows the source timestamp, not a pixel comparison of the selected region.

`timeout` is in seconds: `None` waits indefinitely, `0` polls, and a finite nonnegative value bounds the wait. It does not limit frame conversion time. When no eligible frame is available, the methods return `None`; `get_latest_frame_into(dst, after_timestamp=..., timeout=...)` leaves `dst` unchanged. Worker failures are reported by `stop()`. Omitting these options preserves the latest-frame behavior above.

> When `start()` capture is running, calling `grab()` reads from the in-memory frame buffer instead of directly polling the capture backend.

### Safely Releasing Resources
`release()` stops capture, frees buffers, and releases capture resources.
After `release()`, the same instance cannot be reused.
In-flight readers retain their staging surfaces until readout completes, including across stop or output recovery.

In the unreleased development branch, `stop()` interrupts the producer's pacing
wait and cancels display-recovery retries, including their backoff wait. A
subsequent `start()` retries unfinished recovery before acquiring another frame.
A native capture call already in progress must still return before the worker
can stop.

```python
camera = dxcam.create(output_idx=0, output_color="BGR")
camera.release()
# camera.start()  # raises RuntimeError
```
Equivalently you can use context manager:
```python
with dxcam.create() as camera:
    frame = camera.grab()
# resource released automatically
```

**Full API Docs:** [https://ra1nty.github.io/DXcam/](https://ra1nty.github.io/DXcam/)

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
- `RGB`, `BGR`, `RGBA`, `GRAY` require conversion (`cv2`, `cython`, or compiled `numpy` backend).

### Frame Buffer
DXcam uses a fixed three-slot latest-only frame buffer in-memory. Readers consume the newest published frame. A surface being read is never overwritten; if no safe write slot is available, capture skips that cycle. Older surfaces are released only after their readers finish.

```python
camera = dxcam.create()
```

### Target FPS
DXcam uses high-resolution pacing with drift correction to run near `target_fps`.

```python
camera.start(target_fps=120)  # default: 60
```

In the unreleased development branch, `target_fps` must be a nonnegative Python
`int`. `0` keeps capture unpaced. Booleans, floats (including `60.0`) and negative
values raise `ValueError` before startup changes capture state. A positive FPS is
a target; actual delivery also depends on the desktop, capture backend and workload.

In that branch, all supported Python versions use a native high-resolution timer
and a separate stop event. Older Windows builds that reject the high-resolution
timer flag fall back to a regular waitable timer. `stop()` wakes the pacing wait,
including at low capture rates; it does not interrupt a native capture call
already in progress. Missed ticks are dropped rather than replayed in a burst.
See the [timer pacing comparison](benchmarks/timer_pacing_comparison.md) for
validation and measurements.

#### Native Acquisition Wait (Unreleased)

The unreleased `start(frame_timeout_ms=0)` option sets the maximum native acquisition wait in milliseconds. The default is `0` (polling). Valid values are integers from `0` through `1000`, excluding booleans. A positive timeout lets acquisition return as soon as a frame arrives; it does not add a fixed sleep to each frame.

When `target_fps` is positive, the wait is capped at one nominal frame period, rounded down to whole milliseconds. An explicitly requested `frame_timeout_ms=10` becomes 8 ms at 120 FPS and 4 ms at 240 FPS; the default remains `0`. With `target_fps=0`, timer pacing is disabled and the full requested acquisition wait applies. This producer setting uses milliseconds; the consumer's `get_latest_frame(timeout=...)` uses seconds and only bounds waiting for an eligible published frame.

For **DXGI**, use these settings as starting points:

| Use case | Example `camera.start(...)` arguments | Tradeoff |
| --- | --- | --- |
| Realtime vision or interactive capture | `target_fps=120, frame_timeout_ms=0` | Timer pacing limits polling CPU cost while retaining low frame age. |
| Latency priority, CPU budget available | `target_fps=0, frame_timeout_ms=0` | Consistently low frame age and tighter tails in our test, at roughly one logical core even when idle. |
| Idle or background capture that must remain unpaced | `target_fps=0, frame_timeout_ms=1` | Consider an explicit 1–10 ms wait if increased frame age and tail latency are acceptable; use paced polling when a fixed capture target suits the workload. |
| 60 FPS recording | `target_fps=60, frame_timeout_ms=0` | Compare an explicit 10 ms wait on your setup: it improved typical frame age in our test, but increased CPU use and tail latency. Pace the video writer separately. |

These recommendations come from [DXGI acquisition-wait measurements](benchmarks/acquisition_wait_comparison.md) on one RTX 3060 Ti setup with approximately 60 FPS desktop delivery. No timeout was best for every use case. Benchmark your own workload; these measurements do not establish recommendations for WinRT.

### Frame Timestamp
Read the most recent frame timestamp (seconds):
```python
camera.start(target_fps=60)
frame, ts = camera.get_latest_frame(with_timestamp=True)
camera.stop()
```

For `backend="dxgi"`, this value comes from `DXGI_OUTDUPL_FRAME_INFO.LastPresentTime`.
For `backend="winrt"`, this value is derived from WinRT `SystemRelativeTime`.

In the unreleased development branch, DXGI pointer-only updates are ignored after the initial image in each capture session. That first image may use the mouse-update timestamp or a performance-counter fallback when no presentation timestamp is available. WinRT can include the cursor in the captured pixels, so cursor movement can still produce new frames.

### Video Mode
With `video_mode=True`, DXcam continues publishing at target FPS, reusing the previous frame when no new frame is rendered.

```python
import cv2
import dxcam
import time

target_fps = 30
camera = dxcam.create(output_color="BGR")
camera.start(target_fps=target_fps, video_mode=True)

writer = cv2.VideoWriter(
    "video.mp4", cv2.VideoWriter_fourcc(*"mp4v"), target_fps,
    (camera.width, camera.height),
)
try:
    next_frame = time.perf_counter()
    for _ in range(600):
        time.sleep(max(0, next_frame - time.perf_counter()))
        next_frame = time.perf_counter() + 1 / target_fps
        frame = camera.get_latest_frame()
        if frame is not None:
            writer.write(frame)
finally:
    camera.release()
    writer.release()
```

Latest-frame reads return immediately once a frame exists. Pace the consumer as
above to avoid filling the video with repeated reads as fast as Python can run.
Repeated video-mode frames retain their original source timestamp, so they do
not satisfy an `after_timestamp` threshold equal to that timestamp.

### Capture Backend
DXcam supports two capture backends:
- `dxgi` (default): Desktop Duplication API path with broad compatibility.
- `winrt`: Windows Graphics Capture path.

Use it like this:
```python
camera = dxcam.create(backend="dxgi")
camera = dxcam.create(backend="winrt")
```

Guideline:
- If you need cursor rendering, use `winrt`.
- Start with `dxgi` for most workloads, especially one-shot grab.
- Try `winrt` if it performs better on your machine or fits your app constraints.

Both backends return pixels in desktop orientation, with regions expressed in
output-local desktop coordinates. In the unreleased development branch, WinRT
frames use their existing orientation; DXGI surfaces receive the required monitor
rotation. `camera.rotation_angle` still reports the monitor's rotation. See the
[geometry validation notes](docs/capture-geometry-validation.md) for coverage and
hardware-validation limits.

In the unreleased development branch, WinRT frame-size changes trigger coordinated
recovery of the camera geometry, region, staging buffers and capture session
before another image is copied. The default full-output region follows the new
size; a custom region is clamped when necessary.

`DXCAM_WINRT_DIRTY_REGION_MODE=report_and_render` is rejected with `ValueError`
before WinRT capture-session setup in the unreleased branch: that mode supplies
partial frames, which DXcam does not reconstruct. Leave the variable unset or use
`default` or `report_only` for complete frames.

### Processor Backend
DXcam capture backends (`dxgi`/`winrt`) acquire raw BGRA frame. The processor backend then handles post-processing:
- optional rotation/cropping preparation
- color conversion to your `output_color`

Recommended backend choice:
- OpenCV installed: use `cv2` (default)
- No OpenCV installed: use `numpy`/`cython`

Use it like this:
```python
camera = dxcam.create(processor_backend="cv2")
camera = dxcam.create(processor_backend="cython")
camera = dxcam.create(processor_backend="numpy")
```

Official Windows wheels already include the compiled Cython processor kernels.

Only for source installs:
```bash
set DXCAM_BUILD_CYTHON=1
pip install -e .[cython] --no-build-isolation
```

If `processor_backend="numpy"` is selected but compiled kernels are unavailable,
DXcam logs a warning and falls back to `cv2` behavior. In that fallback path,
install OpenCV for non-`BGRA` output modes.

If `processor_backend="cython"` is selected but compiled kernels are unavailable,
DXcam raises a runtime error because that backend is explicitly the direct
Cython path.

## Benchmarks
See the [0.4 development comparison against PyPI 0.3.0](https://github.com/ra1nty/DXcam/blob/dev/benchmarks/capture_comparison.md)
for measured fresh-frame throughput, frame age, CPU use, and reproducible steps.

When using a similar logic (only capture newly rendered frames) running on a 240fps output, ```DXCam, python-mss, D3DShot``` benchmarked as follow:

|             | DXcam  | python-mss | D3DShot |
|-------------|--------|------------|---------|
| Average FPS | 239.19 :checkered_flag: | 75.87      | 118.36  |
| Std Dev     | 1.25   | 0.5447     | 0.3224   |

The benchmark is across 5 runs, with a light-moderate usage on my PC (5900X + 3090; Chrome ~30tabs, VS Code opened, etc.), I used the [Blur Buster UFO test](https://www.testufo.com/framerates#count=5&background=stars&pps=960) to constantly render 240 fps on my monitor. DXcam captured almost every frame rendered. You will see some benchmarks online claiming 1000+fps capture while most of them is busy-spinning a for loop on a staled frame (no new frame rendered on screen in test scenario).

### For Targeting FPS:
|   (Target)\\(mean,std)          | DXcam (4k)  | python-mss | D3DShot (1080p) |
|-------------  |--------                 |------------|---------|
| 60fps         | 59.99, 0.04 :checkered_flag: | N/A     | 47.11, 1.33  |
| 30fps         | 30.00, 0.00 :checkered_flag:  | N/A     | 21.24, 0.17  |


## Work Referenced

[OBS Studio](https://github.com/obsproject/obs-studio) - implementation ideas and references.

[D3DShot](https://github.com/SerpentAI/D3DShot/) : DXcam borrowed some ctypes header from the no-longer maintained D3DShot.
