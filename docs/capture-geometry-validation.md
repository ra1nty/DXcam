# Capture geometry and pixel validation

Validated September 17, 2026, on changes based on merged `dev`
`f27d45971029a2afde5d67bd789d912f9b391ee0` (PR #148). This is an unreleased
correctness change. It does not establish a performance improvement.

## Backend geometry

Regions and returned pixels use output-local desktop coordinates. Native texture
geometry differs between the two capture APIs:

| Geometry | DXGI duplication | Windows Graphics Capture (WinRT) |
| --- | --- | --- |
| Source/staging dimensions | Unrotated surface; swap desktop dimensions at 90/270 degrees | Desktop-oriented dimensions |
| Rotation applied during processing | Monitor rotation | Zero |
| Public `camera.rotation_angle` | Monitor rotation | Monitor rotation |

Microsoft explicitly documents DXGI's unrotated surface containing a rotated
desktop image. For example, a 768-by-1024 desktop rotated by 90 degrees arrives
in a 1024-by-768 surface. [Desktop Duplication rotation contract](https://learn.microsoft.com/en-us/windows/win32/direct3ddxgi/desktop-dup-api#rotating-the-desktop-image).

The WGC orientation choice is supported by primary implementations, rather than
an explicit orientation guarantee in the Microsoft API pages reviewed here.
OBS applies dimension swaps and rotation transforms only to its DXGI path; its
WGC path renders the captured texture directly.
[OBS monitor rendering](https://github.com/obsproject/obs-studio/blob/caaa0223401f2195128f9998265a0f66d84c9a02/plugins/win-capture/duplicator-monitor-capture.c#L604-L672),
[OBS WGC texture copy](https://github.com/obsproject/obs-studio/blob/caaa0223401f2195128f9998265a0f66d84c9a02/libobs-winrt/winrt-capture.cpp#L132-L175).

Win32CaptureSample also allocates from the capture item's size and copies
the frame without a monitor-rotation transform.
[Win32CaptureSample allocation](https://github.com/robmikh/Win32CaptureSample/blob/49fefe79fd9b11025f0b5eb91783a98888516070/Win32CaptureSample/SimpleCapture.cpp#L35-L45),
[frame copy](https://github.com/robmikh/Win32CaptureSample/blob/49fefe79fd9b11025f0b5eb91783a98888516070/Win32CaptureSample/SimpleCapture.cpp#L135-L150).

The camera now uses the backend's effective geometry consistently for initial
staging, ROI copies, slot allocation, immutable lease metadata, one-shot reads,
and recovery. A retained lease continues using the geometry from its own capture
generation. The public monitor-rotation value remains available to callers.

## Frame-pool size and recovery

WinRT checks frame `ContentSize` against desktop resolution and the dimensions
used to create its frame pool. A mismatch closes the frame and requests the
existing coordinated camera recovery before copying pixels.

Remembering the pool dimensions matters when another camera has already refreshed
the shared output description: matching `ContentSize` and output resolution does
not prove that an older pool contains the complete image. Microsoft notes that
content larger than the pool is clipped, while areas outside smaller content are
undefined. Both growth and shrink therefore require coherent pool, output, ROI,
and staging geometry.
[ContentSize API](https://learn.microsoft.com/en-us/uwp/api/windows.graphics.capture.direct3d11captureframe.contentsize),
[frame-pool sizing guidance](https://learn.microsoft.com/en-us/windows/apps/develop/media-authoring-processing/screen-capture).

## Automated validation

The new tests add 122 cases:

- `tests/test_pixel_reference.py`: 91 cases with asymmetric coordinate patterns,
  literal corner labels, independent channel ordering and grayscale expectations.
  They cover all three processors, five color modes, four rotations, full/edge/
  interior/single-pixel regions, padded source pitch, destination row strides,
  and fallback paths. Source pixels and destination guards must remain intact.
  Twelve cases run the real camera ROI-copy and processing methods together.
- `tests/test_backend_geometry.py`: 31 cases covering real camera one-shot and
  worker publication paths with fake native surfaces, both backends at all four
  rotations, full-output and custom regions, grow/shrink recovery, clamped ROIs,
  retained old-generation leases, and WinRT pool-size tracking.

Grayscale expectations use the documented channel weights and OpenCV 4.13's
15-bit integer conversion implementation, with literal boundary cases. Expected
pixels are not obtained by running another DXcam processor. The geometry fixtures model the
native layouts described above; they cannot establish what a physical capture
API returns on an untested display configuration.

Sixteen selected backend tests were also run against an isolated copy of
`f27d459` with the same compiled processor binaries: **7 failed, 9 passed**.
The failures exposed WinRT's 90/270-degree dimension swap, 180-degree pixel
reversal, rejection of upright portrait content, and acceptance of an old pool
after shared-output refresh. All 31 backend cases pass with the fix.

The complete unit/parity suite passes: **699 tests**, Python 3.14.3, NumPy 2.4.2,
OpenCV 4.13.0.92, compiled NumPy and direct Cython processors. Ruff, Ty, and
format checks on changed Python files pass.

```powershell
python -m pytest -q tests --ignore=tests/benchmarks -p no:cacheprovider
python -m ruff check dxcam tests examples
python -m ty check
```

## Native checks and limits

Bounded local probes inspected the actual acquired `ID3D11Texture2D` description
and WGC item/content dimensions on both outputs of the existing RTX 3060 Ti setup.
Display settings were left unchanged. Post-fix probes used the production size
check without bypassing it; all four backend/output combinations acquired a frame.

| Output | Reported monitor rotation | Desktop resolution | DXGI texture | WGC item / content / texture |
| --- | --- | --- | --- | --- |
| 0 | 0 degrees | 3840 x 2160 | 3840 x 2160 | All 3840 x 2160 |
| 1 | 0 degrees | 2560 x 2880 | 2560 x 2880 | All 2560 x 2880 |

The taller second output also reports zero rotation; its aspect ratio does not
validate a native quarter-turn configuration.

Additional local capture checks passed:

- Concurrent DXGI/WinRT cameras sharing a device, with two readers each, at
  requested producer rates of 0, 120 and 240 FPS; fresh timestamps, destination
  preservation on timeout, blocked-reader shutdown, and restart all passed.
  These requested rates are not measurements of delivered display refresh.
- Both backends at the default zero acquisition timeout decoded a controlled
  visual marker, resumed after slot release, and preserved retained pixels across
  stop/restart until their old staging surfaces could be released.

No captured images were saved. Physical 90/180/270-degree capture, live display
rotation/resizing, cross-adapter migration, DPI variations, and HDR were not
validated in this pass. Before calling rotated-display support hardware-verified,
repeat native item/content/texture measurements and asymmetric corner/ROI pixel
checks at each rotation, including capture recovery during a display change.
