# NumPy pitched-row conversion

Measured September 17, 2026. Baseline: merged `dev`
`dddc5371834aca7ce7f038a5402f77174de57fb6`; candidate: `860b386` on
`codex/numpy-pitched-conversion`. Both use the same Python environment and Cython
build configuration, with each source tree's matching compiled extension.

**Result:** removing the BGRA packing temporary improves the tested padded-row
conversions and eliminates a 31.64 MiB temporary at 4K. The magnitude depends
strongly on OpenMP configuration. These processor measurements do not establish
an equivalent improvement in capture FPS or completed-frame latency.

## Change and scope

For `processor_backend="numpy"`, color conversion can now read packed BGRA pixels
directly from positive padded or cropped rows. Previously, the processor copied
those pixels into a contiguous temporary before converting them. Both
`process()` and `process_into()` use the new path.

The eligible layout has a channel stride of one byte, a pixel stride of four
bytes, and a positive row stride at least as large as the active row. Negative,
transposed, pixel-strided, and channel-strided inputs retain packing fallbacks.
Pitched sources that may overlap the destination still take a snapshot first.
The direct kernel continues requiring a writable, contiguous destination;
the processor preserves its reusable temporary for other destination layouts.

Tightly packed inputs already avoided the source temporary. BGRA output bypasses
color conversion, and rotated color output still uses its prepared BGRA buffer.
The default `cv2` processor, direct Cython processor, capture scheduling, native
acquisition timeout, and public frame-ownership behavior are unchanged. This
change does not fuse rotation and color conversion or add a processed-frame cache.

## Method

The [comparison helper](compare_numpy_pitch.py) runs each version in a fresh
process for three repeats, randomizing version order and pairing the same shuffled
case order within each repeat. It records source/binary hashes, source commits,
the imported extension path, environment, exact case coverage, and completion
status. It refuses to label a run complete if sources or the helper changed.

Inputs are synthetic mapped-memory buffers. Padded rows have 16 extra BGRA pixels;
this is a deliberate test layout, not a claim that the GPU pads these particular
desktop widths. Cases cover 320x180, 1920x1080, 3840x2160, a cropped 1080p view,
packed 1080p rows, and 90-degree processing. Four converted color modes use both
processor APIs. Additional controls cover strided destinations and BGRA output.

Each case warms five times, then repeats fixed-size call batches for at least
0.3 seconds with garbage collection disabled. Call counts can differ between
versions; reported times divide total elapsed time by completed calls. Separate
CPU time covers all process threads and is subject to Windows timer quantization.
The exact output, source contents, and padding guards are checked outside timing.

Allocation tracing measures a separate warmed call after reusable scratch/output
buffers exist. A NumPy allocation probe verifies that array storage is visible
to `tracemalloc`. Incremental peaks are temporary traced allocations, not RSS,
total process memory, VRAM, or GPU power. Internal `process()` can return reusable
storage, so these measurements do not establish public camera allocation costs.
No native acquisition, GPU mapping, display workload, or image files are involved.

Environment: Windows 11 build 26220, AMD Ryzen 9 5900X (24 logical processors),
Python 3.14.3, NumPy 2.4.2, Cython 3.1.1, MSVC optimized/OpenMP build. The primary
run leaves OpenMP environment variables unset and keeps the existing 16,384-pixel
parallel threshold.

## Primary results

All six trials completed with identical case coverage and unchanged source/binary
fingerprints: **54 cases and 324 measurements**. Values below are medians of
three independent trials. Brackets show minimum and maximum wall time.

BGR `process_into()`, with a contiguous destination:

| Input | Baseline microseconds [range] | Candidate microseconds [range] | Speedup | Baseline extra peak bytes | Candidate extra peak bytes |
| --- | --- | --- | --- | --- | --- |
| 320x180 padded | 49.70 [48.77, 52.61] | 20.70 [18.23, 21.76] | 2.40x | 232,016 | 1,520 |
| 1920x1080 padded | 2,129.37 [2,122.64, 2,133.74] | 157.12 [154.13, 176.81] | 13.55x | 8,296,016 | 1,520 |
| 3840x2160 padded | 9,873.08 [9,471.25, 11,780.77] | 668.71 [543.46, 1,496.72] | 14.76x | 33,179,216 | 1,520 |
| 1080p crop | 2,325.30 [2,060.86, 2,374.29] | 167.44 [147.43, 232.26] | 13.89x | 8,296,072 | 1,576 |
| 1080p packed control | 174.06 [147.23, 223.38] | 180.44 [141.27, 208.76] | 0.96x | 1,520 | 1,520 |
| 1080p rotated-90 control | 525.02 [521.52, 872.52] | 576.85 [572.96, 798.04] | 0.91x | 2,224 | 2,224 |

The allocation reduction is one BGRA input image: 8,294,400 bytes at 1080p and
33,177,600 bytes (31.64 MiB) at 4K. Small residual differences include Python/array
metadata. The unchanged controls already reuse their buffers or consume packed
input, so they do not have this frame-sized temporary to remove.

Converted-color improvements occur in both processor APIs and all four tested
color modes. Across padded/cropped layouts, median speedups range from 1.71x to
18.89x. A row-strided 1080p BGR destination improves from 2,860.02 to 471.00 us
(6.07x); its existing reusable destination temporary remains.

This default-OpenMP run is noisy on the control paths. Packed and rotated medians
span 0.85x to 1.30x, with overlapping trial ranges, so they establish neither a
general improvement nor a precise non-regression bound. For example, packed BGR
`process()` is 151.13 versus 178.59 us despite ranges of 143.23-224.67 and
142.24-235.54 us. The large padded-input ratios should not be generalized to every
CPU, thread count, layout, or capture workload.

Median process CPU time per BGR `process_into()` call, summed across all threads:

| Padded input | Baseline CPU microseconds | Candidate CPU microseconds |
| --- | --- | --- |
| 320x180 | 1,115.37 | 419.49 |
| 1920x1080 | 46,549.48 | 3,540.04 |
| 3840x2160 | 203,125.00 | 13,824.46 |

CPU time can exceed wall time because OpenMP uses multiple cores. These are
process CPU measurements, not energy or whole-system utilization estimates.

## Single-thread confirmation

A second run set only `OMP_NUM_THREADS=1`, retaining the existing parallel-path
threshold and the same compiled binaries. Three repeats of 16 BGR/GRAY cases
completed **96 measurements**. This limits the OpenMP worker count; it does not
force the separate serial-kernel branch, which is covered by unit tests.

Padded `process_into()` median wall time:

| Input and color | Baseline microseconds [range] | Candidate microseconds [range] | Speedup |
| --- | --- | --- | --- |
| 1080p BGR | 2,821.05 [2,747.87, 3,238.88] | 1,283.43 [1,259.25, 1,283.76] | 2.20x |
| 4K BGR | 11,312.61 [10,809.27, 11,877.96] | 5,247.04 [5,165.96, 5,575.98] | 2.16x |
| 1080p GRAY | 3,633.84 [3,599.16, 3,770.93] | 2,427.91 [2,300.71, 2,474.42] | 1.50x |
| 4K GRAY | 15,727.99 [15,167.53, 15,866.53] | 9,463.47 [9,272.83, 9,741.83] | 1.66x |

Across both APIs, the padded-case gains are 1.50-2.64x, with the same allocation
reduction. The benefit therefore persists with one OpenMP worker, but the much
larger default-run ratios cannot be attributed to byte traffic alone. They also
depend on allocation, working set, and the interaction with the thread runtime;
this experiment does not isolate those individual causes.

Single-thread controls remain variable: packed median ratios are 0.98-1.18x;
rotated ratios are 0.88-1.00x. Rotated GRAY `process_into()` is 5,511.91 versus
6,234.94 us (ranges 5,334.90-6,348.25 and 5,687.15-6,578.84). These overlapping
ranges do not prove a tight non-regression bound. No global thread-count or
processor-default change is justified by this one-machine comparison.

## Validation

All 757 unit/parity tests pass with the rebuilt extension, including 58 new cases
covering serial/parallel pitched input, odd byte pitch, read-only sources,
unsupported-stride fallbacks, overlap snapshots, destination restrictions, and
empty/single-row arrays. Ruff and Ty pass.

The expanded fixtures exposed a mistake in the earlier independent grayscale
reference: rounding all three decimal weights separately produced coefficients
summing to 32769. The reference now derives blue as the residual after quantizing
red/green, matching the normalized 32768 total. Literal BGR samples
`[254, 218, 19] -> 163` and `[64, 184, 208] -> 177` catch that error. Production
grayscale arithmetic did not change.

Separate native NumPy/BGR checks passed for DXGI and WinRT on the existing
RTX 3060 Ti setup: a 319x180 ROI had a measured 1280-byte pitch for 1276 active
bytes per row. Both decoded the controlled visual marker, resumed after slot
release, preserved retained pixels through stop/restart, and released old stages
after their last reader. Display settings were unchanged and no images were
saved. This verifies a real padded-row path; it is not a native FPS benchmark.

## Reproduction

Export `dddc537` to a separate source root, write its full commit to
`provenance.json` as `{"git_head": "<full SHA>"}`, and build that tree's extension
with the same environment and compiler as the candidate. Keep both trees and
their compiled modules fixed while the benchmark runs. The local baseline export
is `.test/numpy_baseline`.

```powershell
$env:DXCAM_BUILD_CYTHON = "1"
python setup.py build_ext --inplace
python -I benchmarks/compare_numpy_pitch.py --baseline-root .test/numpy_baseline --controls
python -I benchmarks/compare_numpy_pitch.py --baseline-root .test/numpy_baseline --threads 1 --layouts hd_padded 4k_padded hd_packed hd_rot90 --colors BGR GRAY --output benchmarks/results/numpy_pitch_single_thread.json
```

Local raw metrics are written to `benchmarks/results/numpy_pitch.json` and
`benchmarks/results/numpy_pitch_single_thread.json` (ignored by Git). They include
all 420 measurements, call counts, raw wall/CPU durations,
incremental allocation peaks, verification results, and per-file fingerprints.
