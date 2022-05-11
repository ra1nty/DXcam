# DXcam
A Python high-performance screenshot library for Windows using Desktop Duplication API

### **In construction: Everything here is messy and experimental. Features are still imcomplete.**



## Usage
```python
import dxcam
cam = dxcam.create()
frame = cam.capture()
cam.release()
```

## Benchmark
```python
import dxcam
import time

cam = dxcam.create()
start_time = time.perf_counter()
count = 1
while count <= 1000:
    frame = cam.capture()
    if frame is not None:
        count += 1
end_time = time.perf_counter() - start_time

print(f"FPS: {1000/end_time}")
cam.release()
```