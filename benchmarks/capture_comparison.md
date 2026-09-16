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

The authoritative run completed at **23:30:46 UTC** with the final camera-level
processor lock. It compared the published **0.3.0** wheel to the **0.4.0.dev2**
working source, including the pending runtime fixes, on checkout base
`ce4b00fb17aad2f7d52b590bfdf26b37fe021815`.

The source fingerprint before and after all trials matched:
`f39259fcfae5ea4b89687c4d8b4d1896bc4ee0313843d48e99adc9a6c080cf4d`.
This hashes sorted relative paths and contents of every `dxcam/**/*.py` file.
Installed editable distribution metadata still said `0.4.0.dev1`; the harness
records that separately and takes the development version from `pyproject.toml`.
The imported source path was verified to be the current checkout.

Environment: Windows 11 build 26220; AMD Ryzen 9 5900X (24 logical processors);
NVIDIA GeForce RTX 3060 Ti; primary output 3840×2160, rotation 0°; actual capture
region `(64, 64, 1344, 784)` (1280×720). Both workers used Python 3.14.3,
NumPy 2.4.2, OpenCV 4.13.0.92, comtypes 1.4.16 and one OpenCV thread.
The renderer submitted **119.99 updates/s**, while delivered desktop frames were
near 60/s. Requested source FPS is not equivalent to presented source FPS.

**Validity: passed.** All 12 trials completed: 4,426 timestamp-unique frames,
4,381 distinct visual identifiers summed across trials, and zero trial errors,
duplicate reads, missing frames returned, invalid source markers, backwards
timestamps or negative ages. The source stayed unchanged during comparison.
The largest deadline overrun was 29.7 ms on an eight-second trial, caused by
finishing the bounded read/sleep iteration; frames completed after the deadline
were excluded from throughput.

The following values are **medians of three trials**, with each trial's range in
parentheses. Visual FPS counts distinct animation identifiers, while timestamp
FPS counts distinct desktop-present timestamps; unrelated desktop changes can
produce a new timestamp with unchanged animation content.

| Consumer | Version | Timestamp FPS | Visual FPS | CPU % of one core |
|---|---|---:|---:|---:|
| Immediate | 0.3.0 | 60.000 (60.000–60.125) | 59.875 (59.875–59.875) | 13.28 (10.74–13.67) |
| Immediate | 0.4.0.dev2 | 59.875 (59.750–60.000) | 58.125 (56.625–60.000) | 9.18 (6.64–11.72) |
| 30 ms delay | 0.3.0 | 32.375 (32.375–32.375) | 32.375 (32.375–32.375) | 9.74 (7.79–10.12) |
| 30 ms delay | 0.4.0.dev2 | 32.125 (32.125–32.125) | 32.000 (32.000–32.125) | 6.42 (5.26–7.80) |

Frame age is in **milliseconds**, measured from the DXGI last-present timestamp
to completion of the frame read. These are medians/ranges of the individual
trial percentiles, not percentiles pooled across all trials.

| Consumer | Version | p50 age, ms | p95 age, ms | p99 age, ms | Samples |
|---|---|---:|---:|---:|---:|
| Immediate | 0.3.0 | 3.13 (3.12–8.69) | 9.64 (8.61–10.52) | 10.83 (10.19–11.12) | 1,441 |
| Immediate | 0.4.0.dev2 | 3.76 (2.70–6.88) | 7.68 (4.69–10.39) | 7.99 (4.89–10.68) | 1,437 |
| 30 ms delay | 0.3.0 | 14.57 (12.03–15.07) | 22.21 (19.67–22.46) | 22.65 (20.39–23.43) | 777 |
| 30 ms delay | 0.4.0.dev2 | 11.09 (10.70–11.79) | 21.21 (18.28–21.89) | 24.86 (19.08–25.82) | 771 |

Both versions kept up with approximately 60 timestamp-unique desktop frames/s
for the immediate consumer. The development version used less CPU in this run,
but the ranges overlap and three trials do not establish a general CPU saving.
Latency results are mixed: lower development p95 values accompanied a higher
immediate-consumer p50 and a higher slow-consumer p99. Distinct animation delivery
was slightly lower for the development version in this setup. These measurements
do not support a blanket claim that the new runtime is faster or lower latency.

The raw per-trial metrics, settings and source verification are available locally
in `benchmarks/results/capture_comparison.json`, an ignored numeric
artifact that the reproduction command regenerates. A preliminary run before the
processor lock is retained locally as
`results/capture_comparison_pre_processor_lock.json`; it is not the source of the
tables above. No pixel arrays were serialized.

GDI update requests are limited by the desktop compositor and monitor refresh.
This is a controlled real-capture comparison on one machine, not a
hardware-independent maximum FPS or input-to-photon latency claim. Desktop/GPU
activity and scheduling still affect measurements. The test does not compare
WinRT, other processors, rotated monitors, other region sizes or multiple readers.
