# DXGI acquisition timeout benchmark

Measured September 16, 2026 (America/New_York; September 17 UTC), on
`codex/frame-wait-recovery` at `70a3fadd364ccbc00e76268e046d6eb41481ce37`.
This measures the proposed `frame_timeout_ms` control in the current 0.4.0.dev2
source, including the pending fresh-frame API. It is not a comparison of releases.

**Recommendation:** keep DXGI polling (`frame_timeout_ms=0`) as the conservative
default for this pipeline, and expose positive waits as an explicit option.
Positive waits greatly reduce unpaced idle CPU, but can introduce substantial
readout latency through native device-guard contention. Target-FPS pacing already
removes most polling CPU cost. A 60 FPS target benefited in median latency from a
10 ms wait on this display, but consumed more CPU and had worse tail latency.
These results do not support one universally optimal timeout.

Following these measurements, `start()` now defaults to `frame_timeout_ms=0`.
The trials explicitly passed each timeout, so this default change does not alter
their interpretation. The measurements below remain tied to the source commit
and fingerprint recorded here. See the
[use-case recommendations](../docs/migration-0.4.md#native-acquisition-timeout-unreleased).
Addressing the contention remains separate work; native multithread protection
remains enabled.

A subsequent [readout scheduling experiment](readout_scheduling_comparison.md)
tests holding the guard through CPU conversion with a fresh baseline. It reduces
positive-wait frame age on this machine, but remains experimental; production
scheduling and the zero-timeout default are unchanged.

**What the parameter means.** `TimeoutInMilliseconds` bounds how long
`AcquireNextFrame` waits for a new frame; zero polls immediately. A positive value
does not add a fixed sleep to every frame or impose `1000 / timeout` as an FPS
limit. The native wait cannot be cancelled. See Microsoft's
[AcquireNextFrame documentation](https://learn.microsoft.com/en-us/windows/win32/api/dxgi1_2/nf-dxgi1_2-idxgioutputduplication-acquirenextframe).
In this branch, `start(frame_timeout_ms=...)` passes the timeout to DXGI, capped
at `int(1000 / target_fps)` when the target is positive. Thus a requested 10 ms
becomes 8 ms at 120 FPS. The consumer's `get_latest_frame(timeout=...)` is a
separate, seconds-based wait for a published frame.

**Longer confirmation: three 15-second trials per configuration.** Each trial
had two seconds of warmup. Values are medians across trials; age columns are
medians of each trial's percentile, not pooled percentiles. CPU 100% means one
logical core. Target FPS 0 means unpaced capture. Actual desktop delivery was
approximately 60 FPS even though the renderer submitted 120 updates/s.

| Target FPS | Effective wait ms | CPU % one core [min, max] | Delivered source FPS | New visual FPS | Frame age p50 ms | p95 ms | p99 ms |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 0 | 0 | 103.96 [103.69, 106.46] | 59.81 | 59.37 | 1.81 | 2.25 | 5.33 |
| 0 | 10 | 9.06 [7.18, 10.20] | 59.13 | 57.62 | 33.13 | 34.01 | 35.10 |
| 60 | 0 | 7.60 [7.19, 11.25] | 59.87 | 59.75 | 13.68 | 14.28 | 14.54 |
| 60 | 10 | 12.71 [11.67, 12.78] | 59.20 | 58.00 | 1.48 | 2.27 | 33.12 |
| 120 | 0 | 9.79 [7.39, 14.26] | 59.76 | 59.64 | 4.84 | 5.69 | 6.30 |
| 120 | 8 | 9.58 [8.84, 11.56] | 59.20 | 58.87 | 17.59 | 33.76 | 34.19 |

Unpaced 10 ms waiting reduced capture-process CPU by about 91%, while increasing
median frame age from 1.8 to 33.1 ms. At 120 FPS, CPU ranges overlap and the
positive wait gives no convincing CPU benefit; latency is substantially worse.
At 60 FPS, the wait improves p50/p95 but increases the median per-trial p99 from
14.5 to 33.1 ms. Two of the three 60 FPS/10 ms trials had a p99 near 33 ms; the
third was 7.4 ms. Scheduling relative to display presentation matters.

Delivered source FPS counts strictly increasing returned DXGI timestamps.
New visual FPS counts unique decoded renderer IDs, excluding warmup IDs.
Desktop updates outside the region can advance the timestamp without changing
its content, so source FPS alone can overstate useful visual throughput.

**Static-region CPU: three five-second trials each.** No new visual IDs appeared
in any static trial. Eight desktop source updates were delivered across all 33
static trials, consistent with activity outside the frozen region. Static frame
age is not interpreted as capture latency.

| Effective wait ms | Unpaced CPU % [min, max] | 120 FPS target CPU % [min, max] |
| --- | --- | --- |
| 0 | 99.09 [98.71, 99.85] | 0.31 [0.00, 2.50] |
| 1 | 1.25 [0.31, 1.87] | 1.56 [0.94, 1.87] |
| 4 | 1.56 [0.31, 3.12] | 1.87 [0.62, 3.43] |
| 8 | 1.56 [0.94, 2.19] | 1.87 [1.56, 2.19] |
| 10 | 2.19 [1.56, 2.81] | capped to 8 ms |
| 16 | 0.93 [0.62, 1.56] | capped to 8 ms |
| 33 | 0.93 [0.62, 1.25] | capped to 8 ms |

Polling unpaced burns approximately one logical core even with no visual changes.
A small positive wait removes most of that cost, but zero wait combined with
120 FPS pacing is already inexpensive. Differences among low CPU readings are
noisy: roughly 15.625 ms process-time granularity corresponds to 0.31 percentage
points in a five-second trial. A measured zero does not mean literally zero work.

**Memory and stopping.** Across both uninstrumented datasets, process working set
at the end of a trial ranged from 82.46 to 93.16 MiB, with private commit from
897.80 to 902.36 MiB. Animated positive-wait trials generally used only a few MiB
more working set; static working set was mostly near 83 MiB. There is no large
timeout-dependent memory effect in these short runs. Private commit is not
resident RAM or VRAM, and a stable footprint does not rule out allocation churn.
No GPU utilization, GPU allocation, power, or total-system
resource measurement was made. Most stop calls took under one display interval;
the largest was 31.7 ms. Static unpaced 16/33 ms waits had median stop times of
15.7/16.6 ms. These are observations, not shutdown-latency bounds.

**Separate phase diagnostic.** Six additional instrumented, three-second trials
locate delays; they are not included in the CPU or throughput comparison above.
The acquisition hot loop was not wrapped. Map, Unmap, conversion, copy, and entry
to `Device.context_guard()` were timed. The following are per-trial medians:

| Target FPS / effective wait ms | Age at copy start ms | Age at processing start ms | Map guard entry ms | Unmap guard entry ms | Conversion ms | Completed frame age ms |
| --- | --- | --- | --- | --- | --- | --- |
| 0 / 0 | 0.137 | 0.306 | 0.006 | 0.004 | 0.550 | 1.617 |
| 0 / 10 | 0.358 | 16.749 | 0.108 | 15.375 | 0.554 | 33.168 |
| 60 / 0 | 9.989 | 10.111 | 0.004 | 0.003 | 0.496 | 10.957 |
| 60 / 10 | 0.075 | 0.194 | 0.003 | 0.003 | 0.465 | 1.350 |
| 120 / 0 | 6.934 | 7.094 | 0.004 | 0.003 | 0.516 | 7.916 |
| 120 / 8 | 0.317 | 0.776 | 0.067 | 10.416 | 0.534 | 17.486 |

With unpaced 10 ms waiting, the image was copied while still fresh, but processing
started about one display interval later and then spent a median 15.4 ms entering
the native guard before Unmap. The native Unmap call itself added little beyond
guard entry. At 120 FPS/8 ms, Map guard entry also had a 16.0 ms p95, and Unmap
guard entry a 16.2 ms p95. Conversion stayed around half a millisecond. Phase
intervals are nested and their percentiles must not be added together.

This directly locates a substantial delay at device-guard entry. It is consistent
with native DXGI/D3D contention while the producer waits for another frame; it
does not prove which driver/runtime lock `AcquireNextFrame` holds. The explicit
Python device guard does not surround acquisition. Microsoft's
[ID3D11Multithread::Enter documentation](https://learn.microsoft.com/en-us/windows/win32/api/d3d11_4/nf-d3d11_4-id3d11multithread-enter)
describes exclusion involving device and DXGI calls. At 60 FPS, the trace instead
shows that polling acquired an already older presentation; waiting acquired it
promptly without appreciable guard-entry stalls in that short run. Alignment of
the capture timer with presentation is a plausible explanation, not a guarantee.

The useful next experiment is to adjust producer/readout scheduling or ownership
while retaining native protection, then repeat these measurements before selecting
a positive default. Unpaced polling delivered consistently low frame age and
tighter tails, with a clear CPU cost. Paced polling is the conservative
CPU/latency compromise.

**Setup and audit.** Windows 11 build 26220; Ryzen 9 5900X, 24 logical processors;
RTX 3060 Ti, driver 591.86; primary output 3840x2160, secondary 2560x2880. The
capture region was `(64, 64, 1344, 784)` (1280x720), DXGI/BGR/cv2, one OpenCV
thread. Python 3.14.3, NumPy 2.4.2, OpenCV 4.13.0.92, comtypes 1.4.16.

Each uninstrumented trial used a fresh capture process and immediate consumption
with `get_latest_frame(after_timestamp=last, with_timestamp=True, timeout=...)`.
Age is `perf_counter()` immediately after the read returns minus the returned
DXGI presentation timestamp, before marker decoding. It measures presentation
to completed read, not input-to-photon latency. CPU includes capture, conversion,
and marker decoding, excluding the separate GDI renderer, DWM, startup and warmup.
Memory is sampled before and after measurement while capture is still active.
Only metrics were saved; no screenshots were saved.

The initial sweep used three repeats, one-second warmup, five-second measurement,
static and animated content, target FPS 0/120, waits 0/1/4/8/10/16/33 ms, with
equivalent capped configurations deduplicated (66 trials, seed 20260916).
The confirmation used animated content, target FPS 0/60/120, waits 0/10 ms,
three repeats of two-second warmup and 15-second measurement (18 trials,
seed 20260917). Configurations were randomized within each repeat/workload block.
The renderer remained running across configurations in a block. Renderer logs
confirmed one draw for each static block and approximately 120 submissions/s for
animated blocks. This is one GPU/driver/display setup, not a hardware-wide result.

All 84 primary trials completed: zero errors, invalid markers, nonincreasing
timestamps, or negative frame ages. They delivered 25,875 source updates in total.
The six diagnostic trials also reported zero invalid markers or duplicate/backward
timestamps. Runtime source fingerprints matched throughout both primary runs:
`d62f272b4edac89821a86d8fcf58f61a9cd649a02dd66d9c81f00fd4880aebea`.
This hashes sorted relative paths and contents of `dxcam/**/*.py` files.
The measured benchmark script hash was
`7852028a1fec3528ca20d3f195f13fdf7f0ca80ea757fa7d19a4856c86e9a3e0`;
renderer hash was
`1a8ac897b62289ae2366234dfab481d2ec7ee60665eb99a57282ba1649d776c5`.
A lint-only import comment was added to the benchmark after measurement.

A read completing at or after the deadline is excluded from counts and age
samples, while elapsed time and CPU include it. This can exclude one boundary
frame per trial (approximately 0.2 FPS at five seconds or 0.067 FPS at 15 seconds).
The elapsed-time overrun additionally lowered reported source FPS by up to
0.389 FPS in the initial sweep and 0.138 FPS in confirmation relative to dividing
the same counts by nominal duration. Small throughput differences should not be
overinterpreted. The largest observed final overrun was 35.0 ms. The 16-bit renderer ID can wrap
in long runs; these blocks remained far below 65,536 submissions. Warmup IDs are
subtracted so a static seed image is not counted as new visual content.

**Reproduce from this checkout with its development environment.** Run captures
sequentially on an unlocked, visible desktop. Do not run competing captures or
change runtime source during measurement. The harness imports `compare_capture`,
which requires Python 3.11+ for `tomllib`. Install the project's cv2 extra.

```powershell
.venv/Scripts/python.exe -I benchmarks/compare_acquisition_wait.py --output benchmarks/results/acquisition_wait.json
.venv/Scripts/python.exe -I benchmarks/compare_acquisition_wait.py --workloads animated --target-fps-values 0 60 120 --timeouts-ms 0 10 --duration 15 --warmup 2 --repeats 3 --seed 20260917 --output benchmarks/results/acquisition_wait_confirmation.json
.venv/Scripts/python.exe benchmarks/summarize_acquisition_wait.py benchmarks/results/acquisition_wait.json benchmarks/results/acquisition_wait_confirmation.json
.venv/Scripts/python.exe benchmarks/profile_acquisition_wait.py --source-fps 120 --duration 3
```

Local ignored raw metrics: [initial sweep](results/acquisition_wait.json),
[confirmation](results/acquisition_wait_confirmation.json), and
[phase diagnostic](results/acquisition_wait_phases.json). The diagnostic was
originally run from `.test/profile_acquisition_wait.py`; its promoted benchmark
helper preserves the instrumentation, with formatting and provenance improvements.

**Animated initial sweep, three five-second trials per row.** This shorter sweep
shows intermediate wait values; use the longer confirmation above for the 0/10 ms
comparison. Variation between runs reinforces the role of timer/display alignment.

| Target FPS | Effective wait ms | CPU % one core [min, max] | Source FPS | Visual FPS | Age p50 ms | p95 ms | p99 ms |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 0 | 0 | 104.02 [98.64, 107.73] | 59.93 | 59.78 | 1.69 | 2.05 | 5.03 |
| 0 | 1 | 9.97 [4.67, 14.35] | 59.80 | 59.80 | 17.59 | 31.79 | 32.95 |
| 0 | 4 | 7.80 [5.59, 9.98] | 59.61 | 59.61 | 28.26 | 33.63 | 33.96 |
| 0 | 8 | 9.37 [6.54, 12.49] | 58.16 | 58.16 | 32.58 | 33.68 | 37.54 |
| 0 | 10 | 5.62 [5.61, 8.42] | 59.60 | 59.40 | 33.25 | 33.80 | 34.32 |
| 0 | 16 | 6.23 [5.61, 8.41] | 59.21 | 59.21 | 33.44 | 33.94 | 34.25 |
| 0 | 33 | 7.19 [6.54, 9.69] | 58.81 | 58.81 | 33.48 | 34.23 | 50.04 |
| 120 | 0 | 6.55 [6.25, 6.56] | 59.77 | 59.37 | 8.78 | 9.20 | 9.34 |
| 120 | 1 | 11.54 [9.99, 15.31] | 59.78 | 59.78 | 6.54 | 16.99 | 17.44 |
| 120 | 4 | 9.95 [9.34, 12.18] | 59.58 | 59.58 | 13.30 | 33.43 | 33.79 |
| 120 | 8 | 11.84 [3.74, 13.71] | 59.41 | 59.41 | 27.90 | 33.72 | 33.99 |
