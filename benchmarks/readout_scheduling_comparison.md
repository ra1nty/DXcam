# DXGI readout scheduling experiment

Measured September 17, 2026, on `codex/frame-wait-recovery` at
`5d68b04212df8aad52d830a553055344b20b8246`, after the four correctness fixes
from the [investigation](../docs/performance-accuracy-investigation.md).
This compares two readout schedules in the same development checkout, not releases.

**Decision:** retain current production scheduling and the zero acquisition
timeout default. Holding the native device guard through CPU conversion reduces
completed-frame age with positive acquisition waits on this machine, but does
not establish an overall benefit for polling. It also serializes CPU conversion
against acquisition and other cameras sharing the device. The candidate remains
an isolated benchmark experiment, not a new runtime option.

## Candidate and measurement

Production `StageSurface.mapped()` keeps the stage mapping lock for the entire
readout, but enters the native device guard separately for Map and Unmap. The
candidate holds that guard continuously across Map, CPU conversion, and Unmap.
It retains the existing nested guards and the lock order
`processor -> stage mapping lock -> device guard`. Native multithread protection,
reader/writer leases, acquisition waits, and GPU ROI copies remain enabled.

This tests the combined scheduling change and its additional Enter/Leave pair;
it does not isolate the cost of each component. The prior
[phase measurements](acquisition_wait_comparison.md) motivated it by showing
acquisition-related guard-entry delays before Unmap. Holding the guard avoids
that re-entry interval, but cannot remove waits before Map or eliminate the
tradeoff between blocking acquisition and readout.

Both modes reuse the same acquisition-wait measurement worker with no per-frame
profiling hooks. Each trial runs in a fresh process with an immediate consumer;
configurations are shuffled within each repeat. A controlled 1280x720 window
requests 120 updates/s; actual desktop delivery is about 60 FPS. The ROI is
`(64, 64, 1344, 784)`, captured with DXGI, BGR output, and single-threaded OpenCV.

Hardware/software: NVIDIA RTX 3060 Ti, Windows 11 build 26220, Python 3.14.3,
NumPy 2.4.2, OpenCV 4.13.0.92, comtypes 1.4.16; primary output 3840x2160,
secondary output 2560x2880. This is the same workstation as the timeout report.

- CPU 100% means one logical core; the metric includes capture, conversion, and
  marker decoding. Renderer, DWM, GPU utilization, and power are not measured.
- Frame age is DXGI source timestamp to completed read, before marker decoding.
  It is not renderer-submission-to-read latency or an end-to-end latency measure.
- Source FPS counts strictly increasing returned source timestamps. Visual FPS
  counts distinct decoded marker IDs, excluding IDs seen during warmup. A source
  update can occur without a change inside the ROI.
- Tables give medians across three trials, including the median of each trial's
  age percentile rather than pooled percentiles. CPU ranges show trial variation.
- The shared worker excludes a read finishing at/after the deadline, but includes
  its time in elapsed CPU/FPS denominators. Short-trial throughput can therefore
  be biased downward by roughly one frame plus any final-read overrun.

## Five-second grid

One second of warmup, three repeats, 24 trials total. Target FPS 0 is unpaced.
Requested acquisition waits are 0/10 ms; 10 ms is capped to 8 ms at target 120.

| Target FPS | Wait ms | Readout | CPU % [min, max] | Source FPS | Visual FPS | Age p50 ms | p95 ms | p99 ms |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 0 | 0 | baseline | 105.25 [101.56, 106.75] | 59.93 | 59.93 | 1.56 | 1.99 | 4.83 |
| 0 | 0 | hold_guard | 101.46 [101.24, 103.08] | 59.59 | 59.59 | 1.42 | 1.92 | 4.07 |
| 0 | 10 | baseline | 6.87 [5.62, 10.31] | 59.20 | 59.20 | 33.14 | 33.92 | 34.19 |
| 0 | 10 | hold_guard | 9.36 [9.35, 12.15] | 59.73 | 59.73 | 16.98 | 17.73 | 17.91 |
| 120 | 0 | baseline | 8.75 [6.86, 13.73] | 59.69 | 59.69 | 2.40 | 9.12 | 9.37 |
| 120 | 0 | hold_guard | 12.16 [10.93, 13.41] | 59.45 | 59.45 | 3.91 | 4.33 | 4.47 |
| 120 | 8 | baseline | 9.35 [6.25, 9.97] | 58.59 | 58.59 | 17.41 | 33.77 | 34.06 |
| 120 | 8 | hold_guard | 8.43 [5.62, 10.93] | 58.13 | 57.73 | 16.25 | 17.80 | 20.26 |

The unpaced positive-wait improvement is substantial, while polling changes are
small or mixed. At target 120 with zero wait, the candidate improves the median
per-trial tail percentiles but has worse median age and a higher median CPU
reading. Individual paced baseline p50 values span 1.49–8.68 ms, illustrating
presentation-phase sensitivity. These short polling trials do not justify a
default scheduling change.

## Longer positive-wait confirmation

Two seconds of warmup, three repeats, 12 trials total, using a new randomized
order. This follow-up concentrates on the configurations with the clearest
tail-age improvement, not polling.

| Target FPS | Wait ms | Readout | CPU % [min, max] | Source FPS | Visual FPS | Age p50 ms | p95 ms | p99 ms |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 0 | 10 | baseline | 9.48 [8.22, 18.22] | 58.46 | 56.67 | 33.17 | 33.94 | 34.27 |
| 0 | 10 | hold_guard | 8.33 [7.81, 9.17] | 59.39 | 58.13 | 17.04 | 17.76 | 18.23 |
| 120 | 8 | baseline | 8.22 [7.71, 9.99] | 59.20 | 58.82 | 17.91 | 33.81 | 34.23 |
| 120 | 8 | hold_guard | 8.84 [6.67, 9.48] | 59.80 | 59.80 | 16.16 | 17.63 | 17.96 |

The roughly 16 ms improvement in positive-wait p95/p99 persists. CPU ranges
overlap, and the short-run median CPU increase for unpaced waiting does not
persist here; neither run establishes a consistent CPU saving. Visual throughput
is slightly better in the longer medians, but the short grid had the opposite
paced result. The experiment does not establish a universal throughput gain.

One baseline unpaced/10 ms trial had p50 5.61 ms and CPU 18.22%, versus p50 near
33 ms in the other two. Its p95 remained 33.72 ms. The candidate's p50 was
17.04–17.12 ms in all three trials, so it does not improve median age in every
trial; its positive-wait tail improvement is more consistent. The baseline
unpaced p99 values were 34.27, 42.14, and 34.16 ms. These observations reinforce
the scheduling/presentation sensitivity and the need to report trial variation.

Across all 36 trials, final process working set was 90.04–93.70 MiB and private
commit 897.88–902.72 MiB, with no clear large memory difference. These are process
metrics, not GPU memory or allocation-churn measurements. The largest measured
`stop()` duration was 7.33 ms; this is an observation under animation, not a
shutdown bound for other workloads or disconnects.

## Validation and provenance

The initial grid completed all 24 trials with 7,118 returned source updates,
zero invalid markers, nonincreasing timestamps, or negative frame ages, and
three clean renderer exits. The longer confirmation also completed all 12 trials
with 10,658 source updates, zero counter violations, and three clean renderer
exits. Exact configuration coverage and CPU/rate/summary calculations were
independently audited. Raw JSON retains trial summaries rather than per-frame
age samples, so percentiles cannot be reconstructed from those files alone.
Python/Cython/compiled-extension fingerprints
matched at the start and end of every trial and across the controller run.
These marker checks cover the controlled image pattern; they do not establish
full-frame pixel parity, HDR fidelity, or behavior during display transitions.

Runtime source and binary fingerprint:
`cc8b3743a0bc76c489c61cb41549287c5133ec50101503ce0fbf9676dc313bf7`.
Python-only source fingerprint:
`691fd32cd10322ef0e0e24c625c0b709b6839482d74ca2a8eaf418e02d7fad44`.
Scheduling helper SHA256:
`05fdd8fbcb74974645ed8ea05169478ef49fa801b633d9be7f615894610f1092`.
Raw JSON records per-file hashes, dependency versions, worker configuration,
trial order, and hashes of the other measurement helpers. Binary fingerprints
are specific to the locally built extensions and are not portable build IDs.

Reproduce with the checkout and compiled kernels held fixed, no other capture
workloads, and the controlled window unobscured:

```powershell
.venv/Scripts/python.exe -I benchmarks/compare_readout_scheduling.py
.venv/Scripts/python.exe -I benchmarks/compare_readout_scheduling.py --duration 15 --warmup 2 --repeats 3 --timeouts-ms 10 --seed 20260919 --output benchmarks/results/readout_scheduling_confirm.json
```

Local raw metrics: `benchmarks/results/readout_scheduling.json` and
`benchmarks/results/readout_scheduling_confirm.json` (ignored by Git).

Before promoting a scheduling change, test slow and multiple readers, cameras
sharing one device, larger ROIs, NumPy/Cython conversion, and actual higher-rate
displays. The useful next design experiment is a bounded acquisition/readout
handoff or a D3D owner thread that keeps CPU conversion outside the device guard.
Its queueing and copy overhead must be measured against completed-frame age and
distinct visual throughput, not capture-loop FPS alone.
