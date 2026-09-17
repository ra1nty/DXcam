# Slot-capacity and cached-copy benchmarks

Measured September 17, 2026. Baseline is merged `dev` at
`846f1ebacd921847de259194ae4f033e43e243b2`; candidate is
`6411c1bfdc87eabd510de445214b26b1ebf68764` on `codex/capacity-cache`.
Both use the same Python environment and unchanged compiled kernels.

**Result:** blocking for reusable staging capacity removes a busy loop under
reader pressure. Copying a cached frame directly into the destination eliminates
a frame-sized temporary and improves cached-hit copy time on this workstation.
These are isolated CPU microbenchmarks, not measurements of screen-capture FPS
or end-to-end latency.

## Changes and behavior

An unpaced producer previously retried immediately when two older staging slots
were retained by readers and the third held the latest frame. It could use a full
CPU core without acquiring or publishing anything. It now waits on a capacity
condition sharing the publication lock. Only a release that makes a current,
non-latest slot writable wakes it; slot replacement and stop also notify it.
Partial, duplicate, latest-slot, and retired-generation releases do not signal
new write capacity. Fresh-frame readers use a separate condition.

Paced capture still returns to its timer and preserves video-mode repeat
publications. Unpaced video mode can publish one repeat before waiting, but
spurious wakes do not inflate repeats or change the source timestamp. Reader
ownership, latest-only publication, and native device protection are unchanged.

When `grab_into(dst, new_frame_only=False)` has no new image and reuses its
one-shot cache, it previously copied the cache to a temporary before assigning
that temporary to `dst`. It now validates `dst` and copies directly from the
cache. Public cached `grab()` still returns an independent array; fresh-frame
cache storage remains independent of the caller's destination. This change
does not optimize newly acquired images or threaded `get_latest_frame_into()`.

## Method

The [benchmark helper](compare_capacity_and_cache.py) launches each trial in a
fresh process, imports from an explicit baseline/candidate source root, and
randomizes their order within three repeats. It records source/binary hashes,
Python and NumPy versions, exact case coverage, and verification outcomes.
Environment: Windows 11 build 26220, AMD Ryzen 9 5900X (24 logical processors),
Python 3.14.3, NumPy 2.4.2. No GPU acquisition, mapping, conversion, renderer, or
image files are involved in these timed measurements.

- **Capacity:** real `CaptureWorker` and `FrameBuffer`, fake staging surfaces,
  two retained older leases and a third latest slot. After 0.1 s warmup, measure
  two seconds of process CPU while the buffer remains full. Release/resume,
  stop/join, and exactly-once resource cleanup are verified outside that window.
- **Cache:** real `DXCamera._grab_into` with acquisition stubbed to return no new
  frame. BGR data, preallocated destination, eight warmup copies, and at least
  8 GiB of logical output per timing trial. Each baseline/candidate case uses the
  same iteration count. GC is disabled during timing for both. Output pixels,
  unchanged cache data, and untouched padding rows are checked outside timing.
- Allocation tracing is a separate single call. A NumPy probe first confirms
  that array storage is visible to `tracemalloc`. Reported extra peak bytes are
  temporary traced memory above the already allocated cache/destination, not
  RSS, VRAM, total process memory, or a general no-allocation guarantee.

## Capacity results

CPU 100% means one logical core. Values summarize three independent trials.

| Version | Median CPU % | Trial range | Captures during saturation | Largest blocked-stop time |
| --- | --- | --- | --- | --- |
| Baseline | 100.10 | 99.56–100.28 | 0 | 0.193 ms |
| Candidate | 0.00 | 0.00–0.00 | 0 | 0.240 ms |

The measured zero means no process-time increment was observed during the
two-second windows, not literally zero work. Windows process-time quantization
limits precision at this scale. This saving requires exhausted writable slots;
it does not remove normal polling CPU when slots are available and the desktop
has no new image. Native acquisition timeout remains zero by default.

All lease-release/resume checks passed. Release to *observed* publication was
about 30–31 ms for both versions in this synthetic hot-producer fixture. That
includes notification observation and interpreter scheduling; it is not an
isolated wake latency or a desktop-frame-age measurement. The fixture establishes
resumption, not a latency improvement or a shutdown guarantee.

## Cached-hit results

Medians across three trials; brackets show the minimum and maximum wall time.
Speedup is baseline median divided by candidate median. Row-strided destinations
use every other row of a larger backing array.

| Size | Destination | Baseline µs/call [min, max] | Candidate µs/call [min, max] | Speedup | Baseline extra peak bytes | Candidate extra peak bytes |
| --- | --- | --- | --- | --- | --- | --- |
| 320×180 | contiguous | 8.30 [8.10, 8.37] | 4.05 [4.00, 4.14] | 2.05× | 172,960 | 64 |
| 320×180 | row-strided | 8.41 [8.31, 8.49] | 4.21 [4.19, 4.60] | 2.00× | 172,960 | 64 |
| 1920×1080 | contiguous | 1268.20 [1232.02, 1297.69] | 225.91 [224.67, 244.14] | 5.61× | 6,220,960 | 64 |
| 1920×1080 | row-strided | 1440.88 [1291.20, 1478.99] | 342.75 [180.28, 374.65] | 4.20× | 6,220,960 | 64 |
| 3840×2160 | contiguous | 4851.39 [4775.89, 4916.26] | 1052.54 [965.30, 1067.70] | 4.61× | 24,883,360 | 64 |
| 3840×2160 | row-strided | 5798.53 [5532.27, 5858.73] | 1662.28 [1620.78, 1881.11] | 3.49× | 24,883,360 | 64 |

Baseline extra peak is one BGR frame plus 160 bytes in every measured case;
candidate extra peak is 64 bytes. At 4K this removes a 24,883,200-byte temporary
(23.73 MiB). Timing improvements need not equal the number of copies removed:
allocation and memory working-set effects also matter. The large 1080p strided
variation is shown explicitly; these results do not establish a universal
speedup across CPUs, resolutions, NumPy versions, or memory layouts.

## Validation

All 42 trials completed with exact configuration coverage, unchanged runtime
and helper fingerprints, passing pixel/padding/cache checks, and clean worker
shutdown. Timing, allocation, CPU, and summary calculations were independently
audited. The source has 577 passing unit/parity/regression tests, including
deterministic missed-wakeup, spurious-wakeup, lease-generation, stop/restart,
paced-repeat, read-only, strided-destination, and cache-ownership cases.

Separate real-hardware smoke tests passed on both DXGI and WinRT at zero target
FPS and the default zero native wait: retaining two older frames stopped further
publication, releasing a lease resumed newer valid frames, and stop/restart
preserved retained pixels until the old leases were released. These check
publication state rather than instrumenting native acquisition. A further
shared-device smoke passed with concurrent ndarray/`_into` readers on both
backends at target FPS 0/120/240. No display rotation or physical disconnect was
performed; those remain separate hardware coverage.

## Reproduction and provenance

Export baseline commit `846f1eb` into a separate source directory and record its
full commit in a `provenance.json` file there as `{"git_head": "<full SHA>"}`.
The measured local export is `.test/perf_baseline`; it contains matching copies
of the unchanged compiled kernels. Use the same virtual environment for both
source roots, hold sources/binaries fixed, and stop competing benchmarks:

```powershell
.venv/Scripts/python.exe -I benchmarks/compare_capacity_and_cache.py --baseline-root .test/perf_baseline --capacity-seconds 2 --copy-mib 8192
```

Local raw metrics are `benchmarks/results/capacity_and_cache.json` (ignored by
Git). The JSON contains each trial's source path, inputs, measurements,
verification flags, and per-file hashes. The runtime fingerprint includes
`.py`, `.pyx`, and `.pyd`; the exported baseline has Git-normalized line endings,
while the candidate uses working-tree line endings, so its distinct digest
also reflects that representation difference.

- Baseline runtime: `2fd8ba1b3fa8bf238e5822115d42e591704321c91c7c4af35e80e79126285939`.
- Candidate runtime: `20e257c2f7d0caa140057f2329a5ce5e92cba996b68fe1a0cc2f49b266397b9b`.
- Helper SHA256: `e6e5a68bba337e90189ef40acf076335f93720143f6ea763a62d8f741de15e5c`.
