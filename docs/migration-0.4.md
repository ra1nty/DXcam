# Migrating from DXcam 0.3 to 0.4

0.4 is currently a development release. Install it explicitly:

```bash
pip install "dxcam[cv2,winrt]==0.4.0.dev2"
```

## Camera creation

Remove `max_buffer_len` from `dxcam.create()` and `DXCamera()` calls. Capture
always uses three staging slots; there is no configurable frame history.
`backend` and `processor_backend` are keyword-only:

```python
camera = dxcam.create(output_color="BGR", backend="dxgi", processor_backend="cython")
```

Importing `dxcam` no longer discovers adapters or creates D3D devices. Discovery
occurs on the first `create()`, `device_info()`, or `output_info()` call. Import
also leaves application logging levels unchanged. The factory still reuses a
live camera for each device/output/capture-backend tuple; call `release()` before
creating one with different parameters.

## Reading frames

`grab()` and `get_latest_frame()` return independently owned NumPy arrays.
`copy=` options, `grab_view()`, and `get_latest_frame_view()` have been removed.
Use `grab_into(dst)` or `get_latest_frame_into(dst)` to reuse a caller-owned,
writable, contiguous `uint8` array of the correct shape:

```python
import numpy as np
import dxcam

with dxcam.create(region=(0, 0, 640, 360), output_color="BGR") as camera:
    dst = np.empty((360, 640, 3), dtype=np.uint8)
    if camera.grab_into(dst):
        # Consume dst before the next grab_into overwrites it.
        pass
```

During threaded capture, readers process the latest staged frame on demand.
They wait until a frame first becomes available, then may immediately read the
same frame again. This applies to `grab()` as well as `get_latest_frame()`;
`new_frame_only` only controls one-shot capture. Compare returned timestamps
when counting fresh frames. `target_fps` paces capture, not consumer calls.

### Waiting for fresh frames (unreleased)

The current development branch adds keyword-only `after_timestamp` and
`timeout` options to `get_latest_frame()` and `get_latest_frame_into()`. These
options are not included in the published `0.4.0.dev2` package.

```python
last_timestamp = None
camera.start()
try:
    for _ in range(1000):
        result = camera.get_latest_frame(
            with_timestamp=True, after_timestamp=last_timestamp, timeout=0.5
        )
        if result is None:
            break
        frame, last_timestamp = result
        # Process frame here.
finally:
    camera.stop()
```

An eligible frame has a source timestamp strictly greater than
`after_timestamp`. Each reader maintains its own threshold; readers do not
consume a shared update flag. This remains a latest-only buffer, so a slow
reader can skip intermediate frames. Source timestamps identify updates, not
whether the pixels within a particular capture region changed.

`timeout` is measured in seconds. `None` waits indefinitely, `0` polls, and
finite nonnegative values bound the wait for an eligible frame. The timeout
does not limit conversion after a frame is selected. A wait that ends without
an eligible frame returns `None`, including on capture stop or worker failure;
`stop()` still raises the worker's error. `get_latest_frame_into(dst, ...)`
leaves `dst` unchanged on `None`, and returns `True` or `(True, timestamp)` on
success. Omitting the new options preserves immediate reads of an existing
frame. An `after_timestamp` threshold must be finite; keep the timestamp
returned with the frame rather than substituting wall-clock time.

Video writers must pace their own sampling at the intended video frame rate.
`video_mode=True` permits repeat publication but does not make every read block
for the next video tick. Repeated frames retain their original source timestamp.
They do not satisfy `after_timestamp` when the threshold equals that timestamp;
use ordinary paced reads when recording those repeats is intentional.
See [capture_to_video.py](../examples/capture_to_video.py) for paced recording.

### Native acquisition timeout (unreleased)

`start(frame_timeout_ms=10)` sets the maximum backend acquisition wait in
milliseconds. Valid values are integers from `0` to `1000`, excluding booleans;
`0` polls. With positive `target_fps`, this is capped at one nominal frame period
rounded down to whole milliseconds. For example, the default becomes 8 ms at
120 FPS and 4 ms at 240 FPS. `target_fps=0` disables timer pacing and uses the
full requested acquisition wait. Producer pacing, native acquisition waits,
and consumer read timeouts are separate controls.

DXGI now suppresses pointer-only updates after the first image in each capture
session. The first image can seed the buffer using a mouse-update timestamp or
performance-counter fallback when no presentation timestamp is available.
WinRT can render the cursor into the captured pixels, so its cursor updates
can still advance the source timestamp.

## Buffer lifetime and recovery

Reader leases keep surfaces alive while conversion runs. Stop and display
recovery retire old surfaces; they free each surface after its last reader or
writer finishes. A read already in progress can complete after `stop()`.
Waiting readers from an old capture session never switch into a restarted one.
When all spare slots are in use, capture skips a cycle rather than overwrite a
published or leased frame. Frame conversion is serialized per camera to protect
processor scratch buffers shared by concurrent readers. Call `start()`, `stop()`,
and `release()` from one controlling thread; concurrent lifecycle mutations are
not supported.

Native Direct3D multithread protection is required. Device initialization raises
a clear error if it cannot be enabled, instead of continuing with unsafe shared
device access.

## Processing backends

`processor_backend="cython"` selects the direct compiled kernels. Windows wheels
include them; source builds need the Cython build described in CONTRIBUTING.
`cv2` and `numpy` remain supported. NumPy is still required, and public outputs
remain NumPy arrays.
