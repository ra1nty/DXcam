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

Video writers must pace their own sampling at the intended video frame rate.
`video_mode=True` permits repeat publication but does not make every read block
for the next video tick. Repeated frames retain their original source timestamp.
See [capture_to_video.py](../examples/capture_to_video.py) for paced recording.

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

## Processing backends

`processor_backend="cython"` selects the direct compiled kernels. Windows wheels
include them; source builds need the Cython build described in CONTRIBUTING.
`cv2` and `numpy` remain supported. NumPy is still required, and public outputs
remain NumPy arrays.
