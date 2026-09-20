### Unreleased
- Use a native high-resolution timer plus a separate stop event on every supported Python version, with a regular waitable-timer fallback on older Windows; `stop()` interrupts pacing waits. See the [timer pacing comparison](benchmarks/timer_pacing_comparison.md).
- Validate `target_fps` before capture startup changes state: require a nonnegative Python integer, reject booleans, floats and negative values, and retain `0` for unpaced capture.
- Close timer handles after the worker's timer wait returns, preserve primary capture errors during cleanup, and still attempt a bounded join if signaling cancellation fails. Native capture calls already in progress must still return before the worker can stop.
- Convert positive row-padded BGRA inputs directly in the NumPy processor, avoiding a frame-sized input temporary while preserving unusual-stride and overlap fallbacks; see the [processor comparison](benchmarks/numpy_pitch_comparison.md).
- Expand exact conversion coverage for pitched and overlapping arrays, and correct the independent grayscale reference's coefficient normalization without changing production color arithmetic.
- Separate WinRT's desktop-oriented frame geometry from DXGI's unrotated surfaces so rotated-monitor capture uses the correct staging dimensions, regions and pixel orientation.
- Validate WinRT content against both output resolution and the frame pool's creation size, including when another camera has already refreshed the shared output.
- Add independent pixel references and backend geometry/recovery regressions; record [geometry evidence and validation limits](docs/capture-geometry-validation.md).
- Wait for reader-held staging slots to become reusable during unpaced capture instead of spinning; stop interrupts the wait and paced video repeats keep their existing behavior.
- Avoid the intermediate array copy when `grab_into(dst, new_frame_only=False)` reuses a cached frame, while keeping cached `grab()` results independently owned.
- Record [capacity-wait and cached-copy measurements](benchmarks/capacity_and_cache_comparison.md), with separate DXGI/WinRT lifecycle smoke checks.
- Keep the selected fallback output alive during display recovery; release unused candidates on both successful selection and exceptions.
- Add per-reader `after_timestamp` filtering and optional waiting deadlines to `get_latest_frame()` and `get_latest_frame_into()`; retain immediate latest-frame reads by default.
- Wake all waiting readers on publication, stop, or worker failure; preserve destination arrays when no eligible frame is available.
- Add `start(frame_timeout_ms=0)` with polling as the default; explicit positive values bound native acquisition waits independently of consumer deadlines and are capped at one frame period when producer pacing is enabled.
- Document DXGI acquisition-wait recommendations by use case, based on [CPU and frame-age measurements](benchmarks/acquisition_wait_comparison.md); positive waits remain an explicit latency/CPU tradeoff.
- Ignore DXGI pointer-only updates after the initial image in each capture session; video-mode repeats retain their original timestamp and do not satisfy fresh-frame reads.
- Route WinRT size changes through coordinated camera recovery so geometry, regions and staging buffers update before the next image copy.
- Reject `DXCAM_WINRT_DIRTY_REGION_MODE=report_and_render` before WinRT session setup; use the default mode or `report_only` until partial-frame reconstruction is supported.
- Require writable destination arrays for `grab_into()` and `get_latest_frame_into()`, and reject read-only destinations in the compiled NumPy kernels before writing.
- Let `stop()` cancel display-recovery retries and wake recovery backoff; a subsequent `start()` retries unfinished recovery before acquisition. Native calls already in progress are not interrupted.

### 0.4.0.dev2
- Keep reader and writer ownership attached to individual staging surfaces, including across stop and display recovery.
- Never overwrite the published or leased frame; skip capture when no safe slot is available.
- Serialize frame conversion to protect shared processor scratch buffers; release retired surfaces after their last owner finishes.
- Keep WinRT frame acquisition outside the D3D context lock and guard surface mapping/copying consistently to prevent capture/readout deadlocks.
- Share native D3D multithread protection across cameras on a device; fail initialization clearly if protection is unavailable.
- Read the latest available frame without waiting for another publication; pace video consumers explicitly.
- Report capture timer setup failures and clean up after worker failures.
- Lazily discover devices on first use, simplify the camera factory, and leave application logging unchanged on import.
- Remove the ignored `max_buffer_len` argument; make backend options keyword-only.
- Add runtime/factory regression tests to source and wheel CI, plus the [0.4 migration guide](docs/migration-0.4.md).
- Add a reproducible [capture comparison against PyPI 0.3.0](benchmarks/capture_comparison.md) covering fresh throughput, frame age, and CPU use.

### 0.4.0.dev1
- Replace the ring buffer with three latest-frame staging slots and move rotation/color conversion to reader time.
- Add a direct Cython processor and `grab_into` / `get_latest_frame_into` APIs.
- Remove copy/view API variants and simplify capture worker and resource ownership.

### 0.3.0
- add proper handling of the DXGI mode switch (exclusively <-> normal)
- add microbenchmark for processors
- refactors
### 0.2.0
- Added a new processor split: cv2 backend / numpy (Cython-kernel) backend
- Added Cython kernels (_numpy_kernels.pyx) for BGRA map/rotate/crop + color conversion, including OpenMP/tuning knobs.
- WinRT backend support， cursor rendering, etc
- Official Doc
- Enhanced examples
### 0.1.0
- Switched frame transfer from full-surface CopyResource to region-aware CopySubresourceRegion
- Made IDXGIOutput5.DuplicateOutput1 the default capture path, with env-var fallback to legacy DuplicateOutput.
- Added explicit DXGI access-lost/session-disconnect handling with safe recovery
- Reduced capture-thread lock hold time
- Optimized NumPy/OpenCV processing
- Added per-frame DXGI timestamp tracking and optional timestamp return
- Optimized timer pacing
- Updated grab() API with optional new_frame_only flag
- Added grab_view() as a zero-copy snapshot helper.
- Changed grab() behavior during active start() capture to read from the ring buffer (instead of polling DXGI directly).
- Overhaul
### 0.0.5
- Fixed black screen for rotated display
- Added delay on start to prevent black screenshot 
- Fixed capture mode for color = "GRAY"
### 0.0.2
- Refactoring
- Screen capturing w/ target FPS use CREATE_WAITABLE_TIMER_HIGH_RESOLUTION
### 0.0.1
- Initial commit
- Basic features: screenshot
