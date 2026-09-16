# Capture comparison: current checkout versus PyPI 0.3.0

This comparison measures timestamp-unique delivered frames, frame age at read
completion, and CPU time for the capture process. It does not interpret repeated
reads of one latest frame as additional captured frames.

## Reproduce

The harness requires Python 3.11+ (`tomllib`). Use the same CPython version for
both environments. Install the published package
in an isolated environment; leave the development environment unchanged. The
versions below match the development environment used when preparing this run.

```powershell
uv --cache-dir .test/uv-cache venv --python .venv/Scripts/python.exe .test/benchmark-030
uv --cache-dir .test/uv-cache pip install --python .test/benchmark-030/Scripts/python.exe dxcam==0.3.0 numpy==2.4.2 opencv-python==4.13.0.92 comtypes==1.4.16
.venv/Scripts/python.exe benchmarks/compare_capture.py
```

The harness uses isolated Python subprocesses (`-I`) and verifies that the
baseline imported the installed 0.3.0 package rather than the checkout. The dev
worker explicitly imports the current source tree. Each worker records its
Python version, dependencies, imported package path and package version.

## Measurement plan

- Both versions use DXGI, `processor_backend="cv2"`, BGR output, one OpenCV
  thread, the same capture region and a 120 FPS capture target.
- A separate, non-activating GDI window supplies a moving bar and a binary frame
  identifier. Its 1280×720 size is reduced if necessary to fit the primary
  monitor. No existing application is controlled, and no captured image is saved.
- Three repeats alternate package order, with two seconds of warmup and eight
  measured seconds each. One scenario consumes immediately; another sleeps
  30 ms after each read to represent a slower downstream consumer.
- Unique DXGI timestamps determine delivered throughput. Duplicate reads are
  reported separately. Source identifiers verify that the controlled window is
  actually visible and expose repeated visual content from unrelated desktop
  updates. Invalid barcode/complement readings flag obscured or invalid samples.
  Source identifiers wrap after 65,536 submissions. The default eight-second
  trial at 120 requested submissions/second is safely below that period; long
  trials that reuse an identifier undercount unique visual content. Timestamp
  uniqueness is independent of this diagnostic identifier.
- Both workers poll the common `latest_frame_time` property and sleep 1 ms while
  it is unchanged before requesting a frame. Returned timestamps are still
  deduplicated. This compares efficient latest-frame consumption and avoids
  inflating CPU with repeated conversion of an unchanged frame. Polling can add
  up to roughly 1 ms plus scheduler delay to measured frame age.
- Frame age is `perf_counter()` at completed read minus the returned DXGI
  timestamp. Report p50/p95/p99 and mean; negative values flag a clock mismatch.
  This measures frame age, not input-to-photon latency. Read-call duration is
  reported separately and may include blocking for the next frame.
- CPU is `process_time / elapsed_wall_time × 100`: 100% means one CPU core.
  It includes capture-thread and readout work, excluding the animation process,
  initialization and warmup. It may exceed 100%.
- Trials are duration-bounded, with an outer process timeout protecting against
  a stalled frame read or teardown. Large deadline overruns/errors invalidate
  simple performance comparisons and are retained in the results. Small sleep
  overruns are reported; only frames completed inside the measurement interval
  contribute to throughput.

## Results: final runtime, 2026-09-16

The authoritative run completed at **23:47:24 UTC** with the final Device-owned
D3D guard and camera-level processor lock. It compared the published **0.3.0**
wheel to the **0.4.0.dev2** working source, including the pending runtime fixes,
on checkout base `1ffe237b3c148efd46cdbd226ca6817544dff6d6`.

The source fingerprint before and after all trials matched:
`81cf459206c31b4535825739b6905cd5bbf728eee7a80a0650c385c2d64eb7b2`.
This hashes sorted relative paths and contents of every `dxcam/**/*.py` file.
Installed editable distribution metadata and `pyproject.toml` both reported
`0.4.0.dev2`. The imported source path was verified to be the current checkout.

Environment: Windows 11 build 26220; AMD Ryzen 9 5900X (24 logical processors);
NVIDIA GeForce RTX 3060 Ti; primary output 3840×2160, rotation 0°; actual capture
region `(64, 64, 1344, 784)` (1280×720). Both workers used Python 3.14.3,
NumPy 2.4.2, OpenCV 4.13.0.92, comtypes 1.4.16 and one OpenCV thread.
The renderer submitted **120.01 updates/s**, while delivered desktop frames were
near 60/s. Requested source FPS is not equivalent to presented source FPS.

**Validity: passed.** All 12 trials completed: 4,418 timestamp-unique frames,
4,367 distinct visual identifiers summed across trials, and zero trial errors,
duplicate reads, missing frames returned, invalid source markers, backwards
timestamps or negative ages. The source stayed unchanged during comparison.
The largest deadline overrun was 26.6 ms on an eight-second trial, caused by
finishing the bounded read/sleep iteration; frames completed after the deadline
were excluded from throughput.

The following values are **medians of three trials**, with each trial's range in
parentheses. Visual FPS counts distinct animation identifiers, while timestamp
FPS counts distinct desktop-present timestamps; unrelated desktop changes can
produce a new timestamp with unchanged animation content.

| Consumer | Version | Timestamp FPS | Visual FPS | CPU % of one core |
|---|---|---:|---:|---:|
| Immediate | 0.3.0 | 59.875 (59.625–60.000) | 59.625 (59.625–60.000) | 9.96 (8.20–10.16) |
| Immediate | 0.4.0.dev2 | 59.750 (59.500–59.875) | 57.125 (56.750–59.750) | 11.52 (8.98–12.50) |
| 30 ms delay | 0.3.0 | 32.375 (32.375–32.375) | 32.375 (32.375–32.375) | 7.99 (7.21–13.85) |
| 30 ms delay | 0.4.0.dev2 | 32.125 (32.125–32.250) | 31.875 (31.750–32.250) | 4.30 (1.56–5.66) |

Frame age is in **milliseconds**, measured from the DXGI last-present timestamp
to completion of the frame read. These are medians/ranges of the individual
trial percentiles, not percentiles pooled across all trials.

| Consumer | Version | p50 age, ms | p95 age, ms | p99 age, ms | Samples |
|---|---|---:|---:|---:|---:|
| Immediate | 0.3.0 | 6.68 (4.91–7.96) | 7.59 (5.93–8.90) | 8.06 (6.40–9.18) | 1,436 |
| Immediate | 0.4.0.dev2 | 6.50 (5.27–7.25) | 7.39 (6.15–8.19) | 7.72 (6.40–8.44) | 1,433 |
| 30 ms delay | 0.3.0 | 12.00 (11.30–16.88) | 19.46 (18.65–24.51) | 20.48 (19.96–25.26) | 777 |
| 30 ms delay | 0.4.0.dev2 | 15.30 (14.82–17.11) | 22.53 (21.88–24.42) | 23.88 (22.94–28.85) | 772 |

Both versions kept up with approximately 60 timestamp-unique desktop frames/s
for the immediate consumer. The development version used more CPU for the
immediate consumer and less CPU for the slower consumer in this run. Immediate
frame-age percentiles were similar, while slow-consumer frame age was higher for
the development version. Distinct animation delivery was also lower for the
development version in this setup. Three trials on one desktop do not establish
a general performance result; these measurements do not support a blanket claim
that the new runtime is faster or lower latency.

The raw per-trial metrics, settings and source verification are available locally
in `benchmarks/results/capture_comparison.json`, an ignored numeric
artifact that the reproduction command regenerates. Earlier development runs are
retained locally as `results/capture_comparison_pre_processor_lock.json` and
`results/capture_comparison_pre_d3d_guard.json`; neither is the source of the
tables above. An intermediate run was stopped when another guard change made it
invalid. No pixel arrays were serialized.

GDI update requests are limited by the desktop compositor and monitor refresh.
This is a controlled real-capture comparison on one machine, not a
hardware-independent maximum FPS or input-to-photon latency claim. Desktop/GPU
activity and scheduling still affect measurements. The test does not compare
WinRT, other processors, rotated monitors, other region sizes or multiple readers.
