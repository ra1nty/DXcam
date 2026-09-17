# Performance and accuracy investigation

Investigated September 17, 2026, at commit
`80254d4ddc01f7d82e6c0c2bf4d1895dea24f9fc` on `codex/frame-wait-recovery`.
CI and docs passed for this commit. The original investigation changed no runtime behavior.
It combines baseline code, bounded in-memory probes, our existing hardware
measurements, Microsoft/NumPy contracts, and pinned upstream implementations.
No new capture session, display reconfiguration, or competitive FPS benchmark
was run during the investigation. Findings labeled "hypothesis" still require
hardware validation. Code line references and reproduced failures below describe
the investigated commit, before the implementation updates listed next.

## Unreleased implementation status

The subsequent implementation addresses these four confirmed gaps:

- WinRT size changes close the mismatched frame and request coordinated camera
  recovery. Camera geometry and region are applied before replacement staging
  buffers are allocated, and capture resumes with a rebuilt session.
- `DXCAM_WINRT_DIRTY_REGION_MODE=report_and_render` raises `ValueError` before
  WinRT session setup. Unset/default and `report_only` modes remain supported;
  partial-frame reconstruction has not been added.
- Public destination validation and compiled NumPy destination checks reject
  read-only arrays before writing. Caller-provided arrays must be writable.
- Worker cancellation reaches display recovery and interrupts its retry backoff.
  If recovery was unfinished, a later `start()` retries it before acquisition.
  Native calls already in progress must still return; cancellation does not
  forcibly interrupt them.

The original findings and evidence remain below for context. The later
[geometry validation pass](capture-geometry-validation.md) separates WinRT's
desktop-oriented pixels from DXGI rotation, checks the actual frame-pool creation
size, and adds independent pixel references. Synthetic regressions cover every
quarter-turn; physical rotated-display validation remains open. Cross-adapter
migration, timer cancellation, and HDR remain separate work. These correctness
changes do not establish a performance improvement.

A subsequent [controlled readout scheduling comparison](../benchmarks/readout_scheduling_comparison.md)
tests holding the native device guard through CPU conversion. It remains an
isolated experiment; production scheduling and the zero-timeout default are unchanged.

A later [capacity and cached-copy follow-up](../benchmarks/capacity_and_cache_comparison.md)
implements a stop-aware capacity wait for unpaced capture and removes the
temporary copy from cached one-shot `grab_into()`. It includes isolated CPU and
allocation measurements plus real DXGI/WinRT lifecycle checks. The broader D3D
handoff, fused NumPy conversion, and processed-frame-cache proposals remain open.

Detached-output fallback selection now retains its selected replacement and
releases unused candidates, including on exceptions.

**Original recommendation:** fix the confirmed output/lifecycle contract gaps first, then
experiment with D3D work scheduling. Preserve the zero acquisition timeout,
native multithread protection, GPU-side ROI cropping, and reader/writer leases.
The largest measured latency problem is native guard contention with positive
acquisition waits. There are also smaller, well-bounded copy/allocation savings.

## Recommended order at the investigated commit

| Priority | Improvement | Evidence | Scope / tradeoff |
| --- | --- | --- | --- |
| First | Coordinate WinRT geometry changes with camera state | Synthetic shrink reproduces stale ROI after successful pool recreation | Correctness fix; also audit backend-specific rotation |
| First | Reject WinRT `report_and_render` until complete-frame reconstruction exists | Current drain/copy algorithm conflicts with partial-frame contract | Affects optional mode; default complete-frame capture is unaffected |
| First | Reject read-only output arrays before native writes | Synthetic NumPy BGR/GRAY conversion changed read-only arrays | Small API/kernel validation fix |
| First | Make recovery cancellation and output selection explicit | Synthetic worker stop does not stop recovery retries | Avoid indefinite workers after disconnect; retain ownership protections |
| Next | Coordinate acquisition and readout through a D3D owner or bounded handoff | Existing phase trace measures long guard-entry waits | Larger scheduling experiment; preserve slow-reader efficiency |
| Next | Remove redundant CPU copies and wait for slot capacity | Visible extra copy paths; unpaced producer can spin without a free slot | Lower-risk, workload-specific improvements |
| Later | Separate GPU readiness from slot ownership | Asynchronous copy followed by immediate blocking map | Potential overlap at a freshness cost; not automatically lower latency |
| Feature work | HDR/color-space policy and richer frame metadata | BGRA8/uint8-only pipeline, unexposed source metadata | Explicit API/format work; not a one-constant optimization |

## Confirmed correctness findings at the investigated commit

**WinRT resize does not propagate to the camera.**
[`_handle_frame_size_change`](../dxcam/core/winrt_duplicator.py) (lines 441–455)
refreshes the backend Output and recreates the pool, returning success with no
updated frame. [`_capture_to_stage`](../dxcam/dxcam.py) (lines 457–469) therefore
skips camera-level recovery. Camera dimensions, region and rotation can remain
stale. A synthetic 1920x1080 to 1280x720 change left the region at
`(0, 0, 1920, 1080)` with zero camera recovery calls. The next source copy box
would exceed the new texture; growth can instead leave part of the output uncaptured.

Coordinate geometry, clamped/custom ROI, stage allocation and published metadata
before accepting another image. Validate copies against actual texture dimensions.
Microsoft requires in-bounds copy boxes and distinguishes WGC content dimensions
from pool texture dimensions. [CopySubresourceRegion contract](https://learn.microsoft.com/en-us/windows/win32/api/d3d11/nf-d3d11-id3d11devicecontext-copysubresourceregion),
[WGC frame sizing and recreation](https://learn.microsoft.com/en-us/windows/apps/develop/media-authoring-processing/screen-capture).

**WinRT partial frames are treated as complete images.**
[`winrt_duplicator.py`](../dxcam/core/winrt_duplicator.py) (lines 238–246) enables
`ReportAndRender`, but lines 387–402 discard intervening frames and the camera
copies the entire ROI. There is no reconstruction. The upstream sample explicitly
copies only valid dirty rectangles in this mode. An in-memory two-update probe
confirmed that draining the left update and retaining a right-only partial frame
leaves undefined pixels in a whole-frame result. Reject the option or explicitly
use `ReportOnly` until there is a canonical reconstructed image and a tested
policy for every skipped delta. [Pinned Win32CaptureSample implementation](https://github.com/robmikh/Win32CaptureSample/blob/49fefe79fd9b11025f0b5eb91783a98888516070/Win32CaptureSample/SimpleCapture.cpp#L153-L214).

**Read-only destinations are inconsistently enforced.**
[`validate_destination_frame`](../dxcam/util/frame.py) (line 61 onward) checks shape
and dtype, but not `dst.flags.writeable`. In a tiny in-memory probe, the NumPy
processor's BGR and GRAY paths overwrote arrays with that flag set to false.
cv2/direct Cython rejected them. Add public writability validation and protect
the compiled NumPy entry points too; test all color/processor combinations with
unchanged destination data on rejection. This preserves the NumPy array contract.
[NumPy array flags](https://numpy.org/doc/stable/reference/generated/numpy.ndarray.flags.html).

**Recovery retries ignore worker cancellation.**
[`DisplayRecoveryHandler.handle`](../dxcam/runtime/display_recovery.py)
(lines 86–144) has an unlimited retry loop and plain sleeps, without a stop input.
A bounded synthetic probe called the real worker's `stop()` during the first
backoff; recovery continued for two further attempts before the probe terminated
it. [`DXCamera.stop`](../dxcam/dxcam.py) (lines 674–679) can then exhaust its
10-second join and correctly refuse unsafe teardown. Pass cancellation through
recovery and use interruptible backoff. Keep indefinitely retrying transient
transitions only while capture is still requested.

Pacing has a related cancellation gap: modern Python uses an uninterrupted
`time.sleep` until the next tick, and worker stop does not signal that wait.
Negative `target_fps` is also accepted and produces a negative period. Validate
FPS before starting and test interruptible pacing at low rates without assuming
an event wait preserves high-resolution timing. These are code-path findings;
no new timer-latency benchmark was run. [Timer implementation](../dxcam/util/timer.py).

Related output-selection gaps: a successful `GetDesc` for a detached output can
prevent fallback enumeration, and the first fallback need not be attached.
Define whether disconnection means wait for the original monitor or select another;
do not silently change that policy. Cross-adapter migration is larger work because
the owning Device and all capture resources must change as a generation.
[`output_recovery.py`](../dxcam/runtime/output_recovery.py),
[DuplicateOutput1 adapter requirement](https://learn.microsoft.com/en-us/windows/win32/api/dxgi1_5/nf-dxgi1_5-idxgioutput5-duplicateoutput1).

## Performance opportunities

**1. Change scheduling before removing synchronization.** Our
[acquisition-wait benchmark](../benchmarks/acquisition_wait_comparison.md) found
unpaced 10 ms acquisition waiting reduced CPU from 104% to 9% of one logical core,
but increased completed-frame age from 1.8 to 33.1 ms. A separate phase trace
measured about 15.4 ms median entry time to the native guard before Unmap, while
conversion remained about 0.55 ms. This problem was measured with positive waits;
it is not evidence that default polling has the same stalls.

Today, the producer copies to a stage and publishes it; a consumer thread maps,
converts and unmaps it while the producer may acquire again. Test a per-device
D3D owner or bounded producer/reader handoff that schedules these operations
together. A demand-aware CPU-frame publication variant is another candidate:
avoid converting every captured frame when there is no consumer. Preserve
multi-camera safety and keep WinRT frame acquisition outside the explicit device
guard. Microsoft's guidance favors coordinated immediate-context/DXGI use, and
native guard entry can exclude DXGI calls.
[D3D threading guidance](https://learn.microsoft.com/en-us/windows/win32/direct3d11/overviews-direct3d-11-render-multi-thread-intro),
[native Enter semantics](https://learn.microsoft.com/en-us/windows/win32/api/d3d11_4/nf-d3d11_4-id3d11multithread-enter).

**2. Remove measured-path copies without changing owned-array semantics.**
[`numpy_processor.py`](../dxcam/processor/numpy_processor.py) (lines 118–128)
materializes a contiguous BGRA input for unrotated pitched/cropped rows. A
pitch-aware fused conversion could avoid that intermediate. The direct Cython
processor already provides an implementation to compare. Also,
[`_grab_into`](../dxcam/dxcam.py) (lines 411–420) gets a copied cached frame and
then copies it into `dst`; an internal copy-to-destination cache path could avoid
the temporary while preserving independent public outputs.

Repeated reads of one staged source currently repeat mapping and conversion.
For several readers or recording repeated frames, consider a bounded processed
frame cache keyed by capture generation and frame identity, returning owned
copies or filling `dst`. For slow readers, on-demand conversion may still be
better. Benchmark `get_latest_frame` against `get_latest_frame_into`, one versus
several consumers, and default/cv2/numpy/cython processors on the current commit.
Historical microbenchmark files do not establish the current backend ranking.

**3. Wait for capacity under reader pressure.**
[`CaptureWorker._run_capture_cycle`](../dxcam/runtime/capture_worker.py)
(lines 88–93) returns immediately when no write slot is available. With
`target_fps=0`, the outer loop immediately retries without acquisition; a positive
native timeout cannot help this path. A capacity/stop condition signaled when
leases are released can avoid this spin. Preserve the rule that neither the
published nor a reader-owned surface is overwritten. Measure this with two slow
readers holding different non-latest slots in the active buffer. The CPU savings
are a hypothesis, not a result from the existing single-reader timeout benchmark.

**4. Treat GPU completion as a separate state.** Three safe slots do not guarantee
the newest issued copy is complete. The copy is asynchronous and current readers
use blocking `IDXGISurface::Map`. A completion query or nonblocking context Map
could select the newest completed copy. The latter requires a correctly typed
`ID3D11DeviceContext::Map` binding; its `DO_NOT_WAIT` flag must not simply be
passed to the existing surface Map API. It also cannot prevent waiting to enter
our native guard. [Asynchronous copy](https://learn.microsoft.com/en-us/windows/win32/api/d3d11/nf-d3d11-id3d11devicecontext-copysubresourceregion),
[context Map contract](https://learn.microsoft.com/en-us/windows/win32/api/d3d11/nf-d3d11-id3d11devicecontext-map).

OBS provides a useful comparison: it stages the current output and maps the
previous slot. Its inspected D3D Map is blocking, so this is overlap through
delayed readback, not evidence of a fence-based implementation. Copying that
policy directly adds roughly one output cycle of age. Evaluate it as a throughput
option against the current freshness preference.
[Pinned OBS staging/readback schedule](https://github.com/obsproject/obs-studio/blob/caaa0223401f2195128f9998265a0f66d84c9a02/libobs/obs-video.c#L868-L913),
[OBS Map implementation](https://github.com/obsproject/obs-studio/blob/caaa0223401f2195128f9998265a0f66d84c9a02/libobs-d3d11/d3d11-subsystem.cpp#L2863-L2877).

**5. Keep smaller hardware-specific optimizations experimental.** Compare whole
texture `CopyResource` against ROI `CopySubresourceRegion` when dimensions match;
retain GPU cropping for genuine ROIs. An optional `MapDesktopSurface` path can
avoid staging only when `DesktopImageInSystemMemory` is true; it needs different
lifetime handling and cannot be assumed available on our discrete GPU.
[Direct desktop mapping contract](https://learn.microsoft.com/en-us/windows/win32/api/dxgi1_2/nf-dxgi1_2-idxgioutputduplication-mapdesktopsurface).
Dirty/move-rectangle filtering could save work for small static ROIs, but metadata
cost, reconstruction, rotation, and source-timestamp semantics require a separate
design. These are not the first changes to make.

## Fidelity and validation

**HDR is a capability gap.** Both capture backends request BGRA8, staging is
BGRA8, and array allocation/processing assumes uint8. Microsoft warns that this
can clip HDR and recommends an FP16 pipeline with intentional HDR storage or
SDR tone mapping. First document the current contract, then design format-aware
staging, dtype and color metadata together. Do not change only acquisition's
format. [Microsoft HDR capture guidance](https://learn.microsoft.com/en-us/windows/apps/develop/media-authoring-processing/screen-capture).

**Portrait WGC needs a hardware check.** WinRT compares ContentSize against
`Output.surface_size`, which applies DXGI's 90/270-degree dimension swap; shared
camera processing also applies the DXGI rotation. WGC capture-item geometry may
already be oriented. A repeated pool recreation or double rotation is plausible,
but was not reproduced on real hardware. Compare actual item size, frame content
size, texture description and labeled corners before assigning each backend an
explicit geometry contract. DXGI's unrotated-surface behavior is documented;
it should not be assumed to define WGC. [DXGI rotation contract](https://learn.microsoft.com/en-us/windows/win32/direct3ddxgi/desktop-dup-api).

**Add independent pixel oracles and provenance.** Existing parity tests cover
padded pitches, crops, rotations and color modes, but comparing processors can
miss shared assumptions. Add known coordinate-grid pixels across the complete
crop/rotation path, output-writability cases, resize/partial-frame scenarios,
and hosted-process DPI settings. Source metadata should distinguish a true
presentation timestamp from an initial seed fallback, source accumulated frames
from delivered frames, slot-pressure skips, capture generation, protected-content
masking, and cursor policy. DXGI pointer-only suppression is useful, but a source
timestamp advance does not establish that pixels inside the ROI changed.
[DXGI frame metadata](https://learn.microsoft.com/en-us/windows/win32/api/dxgi1_2/ns-dxgi1_2-dxgi_outdupl_frame_info).

The benchmark should score completed-read p50/p95/p99 age, distinct visual
updates, capture-process CPU, allocation/RSS/private commit, GPU/DWM load,
correct pixels and shutdown/recovery. Expand to real 60/120/240 Hz delivery,
small ROI/1080p/4K, idle/animation/game load, one/slow/multiple readers, and more
than one GPU vendor. Requested renderer FPS is not actual presented FPS. The
existing results validate one RTX 3060 Ti setup and do not establish GPU power
or total-system costs.

## What the implementation comparisons actually establish

| Implementation examined | Transferable evidence | Boundary |
| --- | --- | --- |
| [OBS DXGI at caaa022](https://github.com/obsproject/obs-studio/blob/caaa0223401f2195128f9998265a0f66d84c9a02/libobs-d3d11/d3d11-duplicator.cpp#L254-L299) | Polls with zero timeout, copies to an owned GPU texture, releases immediately | Early release has production precedent; this does not prove its CPU-array performance |
| [Win32CaptureSample at 49fefe7](https://github.com/robmikh/Win32CaptureSample/blob/49fefe79fd9b11025f0b5eb91783a98888516070/Win32CaptureSample/SimpleCapture.cpp#L153-L214) | Handles partial dirty frames explicitly | Its GPU preview/visualization is not a complete NumPy image API |
| [windows-capture at c7d1064](https://github.com/NiiightmareXD/windows-capture/blob/c7d106448eb9d9b251345c39047711e1cd408ae2/src/dxgi_duplication_api.rs#L583-L670) | Has caller-reusable staging and GPU ROI copies | Default/retained mapped-buffer paths have different allocation and lifetime contracts |

Microsoft recommends releasing a duplicated frame close to the next acquisition,
while OBS releases after copying. Our code documents historical pacing problems
with late release. Keep early release unless an isolated A/B test demonstrates a
better result; neither precedent settles performance across drivers.
[ReleaseFrame guidance](https://learn.microsoft.com/en-us/windows/win32/api/dxgi1_2/nf-dxgi1_2-idxgioutputduplication-releaseframe).

GPU-native outputs could eventually avoid CPU readback for encoders or GPU
inference, but that would be a separate owned-texture/lifetime API. It cannot
eliminate the readback cost for callers who need NumPy arrays. Do not use OBS,
sample-preview, or library README FPS numbers as an apples-to-apples comparison.

Local ignored research notes and bounded probes are under `.test/research_*`.
The four correctness fixes are recorded in the implementation status above.
The readout scheduling experiment is linked above. A bounded D3D handoff and
copy-elimination experiments remain follow-up work.
