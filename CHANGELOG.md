### [Unreleased]
- Removed unnecessary ctypes import in dxcam.py
- Removed unnecessary formatting in dxcam.py (before DXCamera class)
- Removed unnecessary "as ce" in try-except block catching COMError
- Added CONTRIBUTING.md
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
