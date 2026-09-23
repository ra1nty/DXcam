# Native geometry and recovery validation

This follow-up tests the actual DXGI and Windows Graphics Capture APIs with an
independent GDI pattern. It extends the earlier
[synthetic geometry validation](capture-geometry-validation.md), which could
not establish how physical display rotation behaves.

## Reproduce

Run on Windows from a source checkout with DXcam installed and the optional
WinRT/OpenCV dependencies. The default leaves display modes unchanged:

```powershell
python -I benchmarks/validate_native_geometry.py --output-index 1 --output benchmarks/results/native_geometry_current.json
```

The diagnostic briefly covers the selected display with labeled corner colors,
asymmetric bands and a 32-bit epoch marker with inverse bits. It saves only JSON
metadata and sampled pixel values; captured images stay in memory. Use
`--processor numpy` or `--processor cython` to select another installed processor.

To test real rotations and a smaller supported resolution, explicitly opt in:

```powershell
python -I benchmarks/validate_native_geometry.py --output-index 1 --transitions --output benchmarks/results/native_geometry_matrix.json
```

The coordinator tests candidate modes before applying them. A separate hidden
process is armed before any mode change. Temporary changes use flags zero,
without updating the saved registry configuration. After every case, both the
parent and watchdog verify the original dimensions, orientation, desktop
positions, refresh rates and bit depth of all attached outputs. The watchdog
also restores on parent exit, pipe closure or its deadline. Unsupported modes
are reported as skipped, never as tested. Native driver calls cannot themselves
be interrupted by a Python timeout.

## What counts as a pass

Each checkpoint checks the whole returned frame shape and exact known BGR
pixels, including corners, edges, asymmetric features, all epoch bits and their
inverses. It also checks native source/staging sizes and format, public size and
rotation, the backend's effective rotation, and WinRT pool dimensions. A fresh
native output descriptor is observed without updating the shared cached Output
object. Public rotation is compared with DXGI's native rotation enum; DEVMODE
orientation is not assumed to use the same directional convention.

The auto-fullscreen capture workers are already running when a display mode
changes. The same workers must produce a correct new epoch before any stop or
restart. One retained old staging lease per backend is reprocessed using its
original immutable geometry and compared byte-for-byte with its earlier owned
copy. A retired surface must be released after its final lease ends.

Only after those live checks does the harness stop capture and test actual GPU
ROI capture with both `grab()` and `grab_into()`: an asymmetric top-left crop,
bottom-right crop, interior crop and a one-pixel bottom-right crop. It then
restarts and verifies another frame. That worker stays active through the next
transition, including restoration of the original display mode. Custom-ROI
capture during a live transition is a separate, untested case.

WinRT cursor capture and its capture border are disabled in the diagnostic
child, with the initial effective settings recorded. Otherwise the WinRT border
can legitimately appear in concurrent DXGI captures and contaminate the oracle's
edge pixels. The renderer and capture child use per-monitor-v2 DPI awareness;
other DPI contexts and display scale changes are not covered.

## Findings

The first current-mode check exposed that overlay interference: WinRT passed
all 113 full-frame sample checks and eight ROI/API combinations, while DXGI had
26 edge mismatches blended with the capture border. Disabling the border made
both backends pass. The failed result is retained as diagnostic evidence, not
counted as a capture implementation regression.

The first physical 180-degree test then found a real metadata discrepancy.
WinRT returned correctly oriented pixels, but `camera.rotation_angle` remained
zero because the frame dimensions had not changed and no recovery occurred.
DXGI reported 180 degrees. The pixel and metadata assertions are separate, so
correct pixels could not conceal this failure.

The fix queries current rotation when callers read `rotation_angle` on a WinRT
camera that has not been released. It preserves the last known value after
release or a native query failure. Capture and processing retain their existing cached
geometry, with zero WinRT pixel rotation and no new per-frame descriptor query.
A shared output lock protects descriptor reads against pointer replacement and
release during recovery. Reading metadata neither refreshes shared cached
geometry nor initiates capture recovery.

## Hardware results

Validated September 20, 2026, on Windows 11 build 26220, Python 3.14.3 and an
NVIDIA RTX 3060 Ti, using the OpenCV processor and BGR output. The fixed source
and harness were committed at `072b40a8e836057cd6d030c96445986119524424`; every
recorded source/helper/extension hash stayed unchanged during the final runs.

| Output / state | Desktop pixels | DXGI source texture | WinRT source texture | Result |
| --- | --- | --- | --- | --- |
| Primary, 0 degrees, 60 Hz | 3840 x 2160 | 3840 x 2160 | 3840 x 2160 | Passed |
| Secondary, 0 degrees, 59 Hz | 2560 x 2880 | 2560 x 2880 | 2560 x 2880 | Passed |
| Secondary, 90 degrees | 2880 x 2560 | 2560 x 2880 | 2880 x 2560 | Passed |
| Secondary, 180 degrees | 2560 x 2880 | 2560 x 2880 | 2560 x 2880 | Passed after metadata fix |
| Secondary, 270 degrees | 2880 x 2560 | 2560 x 2880 | 2880 x 2560 | Passed |
| Secondary, resized at 0 degrees | 1920 x 1080 | 1920 x 1080 | 1920 x 1080 | Passed |

The secondary display retained 59 Hz throughout. Every changed state was tested
with both capture workers live, followed by another new-epoch checkpoint after
restoring 2560 x 2880 at zero rotation. The original desktop positions were
also restored: primary `(0, 0)`, secondary `(-2560, -558)`.

A separate WinRT-only 180-degree round trip passed, preventing the concurrent
DXGI camera's output refresh from hiding the original defect. Public rotation
reported `0 -> 180 -> 0` while the WinRT recovery count stayed zero throughout;
the stream and its existing surfaces remained valid.

Across the final runs, all 31 backend checkpoints, 248 ROI/API combinations,
and 18 retained-frame comparisons passed. Four retained comparisons continued
using current surfaces (WinRT's same-size rotations); the other 14 retired
surfaces were released after their last reader. All seven cases verified both
parent and watchdog restoration. No requested mode was skipped.

The full automated suite passed **942 tests**, including the 126 harness cases
and eight new live-rotation metadata regressions. Those regressions cover
standalone WinRT, fallback after native errors/release, unchanged DXGI behavior,
zero rotation queries during frame processing, and serialization of pointer
replacement against an in-flight metadata query. Ruff, ty and pdoc passed.
The sdist includes the helper scripts required by its tests; importing the
verifier does not redirect installed-wheel tests to the source checkout.

Local raw reports (ignored by Git):

- `benchmarks/results/native_geometry_secondary_fixed.json`: complete paired-backend matrix.
- `benchmarks/results/native_geometry_primary_fixed.json`: primary current-mode check.
- `benchmarks/results/native_geometry_winrt_standalone.json`: independent WinRT 180-degree check.
- `benchmarks/results/native_geometry_secondary_matrix.json`: original stale metadata failure.
- `benchmarks/results/native_geometry_secondary_current.json` and `native_geometry_secondary_current_v2.json`: border interference and its control.

The standalone check calls the same `run_case()` implementation with only the
WinRT backend and a 180-degree target. The CLI's `--backends winrt --transitions`
option repeats the complete matrix with that backend alone.

## Interpretation limits

These are instrumented correctness checks on known GDI content, not a capture
FPS, latency, GPU-resource or HDR benchmark. Correct sampled pixels do not prove
that every output pixel is correct. The reports preserve source/helper hashes,
compiled-extension hashes, native metadata and restoration results. Changes of
GPU vendor, driver, DPI context, scaling, protected content, HDR, disconnection
and cross-adapter migration require separate validation.
Native emergency restoration after a crashed parent or hung driver was not
exercised; the watchdog protocol has deterministic tests and the hardware runs
verified normal restoration and disarming. Primary-display transitions were
not tested.

The harness records stream continuity separately from completed recovery
generations. A WinRT stream can remain valid through 180-degree rotation
without rebuilding its frame pool; zero recoveries in that case is not a
failure by itself.

## References

- [Desktop Duplication rotation contract](https://learn.microsoft.com/en-us/windows/win32/direct3ddxgi/desktop-dup-api#rotating-the-desktop-image)
- [WGC frame sizing and pool recreation](https://learn.microsoft.com/en-us/windows/apps/develop/media-authoring-processing/screen-capture)
- [DEVMODE orientation fields](https://learn.microsoft.com/en-us/windows/win32/api/wingdi/ns-wingdi-devmodew)
- [Temporary display-mode changes](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-changedisplaysettingsexw)
