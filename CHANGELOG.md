### [Unreleased]
- Added CONTRIBUTING.md
- Fixed broken formatting in README.md
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
