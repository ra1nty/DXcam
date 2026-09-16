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
