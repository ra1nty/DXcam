from __future__ import annotations

import argparse
import ctypes
import gc
import logging
import statistics
import time
from dataclasses import dataclass
from importlib import import_module
from typing import Any

import numpy as np
from numpy.typing import NDArray

from dxcam.processor import Processor, normalize_processor_backend_name
from dxcam.types import ColorMode, ProcessorBackend, Region

logger = logging.getLogger(__name__)


class FakeMappedRect:
    """Minimal DXGI_MAPPED_RECT-like object for processor micro-benchmarking."""

    def __init__(self, pitch: int, pbits: Any) -> None:
        self.Pitch = pitch
        self.pBits = pbits


@dataclass
class BenchResult:
    backend: ProcessorBackend
    mode: ColorMode
    variant: str
    fps: list[float]
    ms_per_frame: list[float]
    gpix_per_s: list[float]
    checksum: int

    @property
    def median_fps(self) -> float:
        return statistics.median(self.fps)

    @property
    def mean_fps(self) -> float:
        return statistics.fmean(self.fps)

    @property
    def median_ms(self) -> float:
        return statistics.median(self.ms_per_frame)

    @property
    def median_gpix_per_s(self) -> float:
        return statistics.median(self.gpix_per_s)


def aligned_pitch(width_pixels: int, align_bytes: int = 256) -> int:
    row_bytes = width_pixels * 4
    return ((row_bytes + align_bytes - 1) // align_bytes) * align_bytes


def make_fake_rect(
    width: int,
    height: int,
    rotation_angle: int,
) -> tuple[FakeMappedRect, Any]:
    rows = height if rotation_angle in (0, 180) else width
    active_cols = width if rotation_angle in (0, 180) else height
    pitch = aligned_pitch(active_cols)
    size = pitch * rows
    raw = (ctypes.c_ubyte * size)()
    buffer = np.frombuffer(memoryview(raw), dtype=np.uint8)
    rng = np.random.default_rng(42)
    buffer[:] = rng.integers(0, 256, size=size, dtype=np.uint8)
    return (
        FakeMappedRect(
            pitch=pitch,
            pbits=ctypes.cast(raw, ctypes.POINTER(ctypes.c_ubyte)),
        ),
        raw,
    )


def output_shape(mode: ColorMode, width: int, height: int) -> tuple[int, int, int]:
    if mode in ("RGB", "BGR"):
        return (height, width, 3)
    if mode == "RGBA":
        return (height, width, 4)
    if mode == "GRAY":
        return (height, width, 1)
    return (height, width, 4)


def _frame_checksum(frame: NDArray[np.uint8]) -> int:
    return int(frame[0, 0, 0]) ^ int(frame[-1, -1, 0]) ^ int(frame[frame.shape[0] // 2, 0, 0])


def _run_once(
    processor: Processor,
    variant: str,
    rect: FakeMappedRect,
    width: int,
    height: int,
    region: Region,
    rotation_angle: int,
    iterations: int,
    mode: ColorMode,
) -> tuple[float, float, float, int]:
    checksum = 0
    n_pixels = width * height

    if variant == "process":
        start = time.perf_counter()
        for _ in range(iterations):
            frame = processor.process(rect, width, height, region, rotation_angle)
            checksum ^= _frame_checksum(frame)
        elapsed = time.perf_counter() - start
    else:
        dst = np.empty(output_shape(mode=mode, width=width, height=height), dtype=np.uint8)
        start = time.perf_counter()
        for _ in range(iterations):
            processor.process_into(rect, width, height, region, rotation_angle, dst)
            checksum ^= _frame_checksum(dst)
        elapsed = time.perf_counter() - start

    fps = iterations / elapsed
    ms_per_frame = (elapsed / iterations) * 1000.0
    gpix_per_s = (iterations * n_pixels) / elapsed / 1e9
    return fps, ms_per_frame, gpix_per_s, checksum


def run_processor_bench(
    processor_backend: ProcessorBackend,
    color_mode: ColorMode,
    variant: str,
    rect: FakeMappedRect,
    width: int,
    height: int,
    region: Region,
    rotation_angle: int,
    warmup: int,
    iterations: int,
    repeats: int,
) -> BenchResult:
    processor = Processor(output_color=color_mode, backend=processor_backend)
    fps_runs: list[float] = []
    ms_runs: list[float] = []
    gpix_runs: list[float] = []
    checksum = 0

    gc_was_enabled = gc.isenabled()
    if gc_was_enabled:
        gc.disable()
    try:
        for repeat_idx in range(repeats):
            for _ in range(warmup):
                if variant == "process":
                    warmup_frame = processor.process(rect, width, height, region, rotation_angle)
                else:
                    dst = np.empty(
                        output_shape(mode=color_mode, width=width, height=height),
                        dtype=np.uint8,
                    )
                    processor.process_into(rect, width, height, region, rotation_angle, dst)
                    warmup_frame = dst
                checksum ^= _frame_checksum(warmup_frame)

            fps, ms_per_frame, gpix_per_s, run_checksum = _run_once(
                processor=processor,
                variant=variant,
                rect=rect,
                width=width,
                height=height,
                region=region,
                rotation_angle=rotation_angle,
                iterations=iterations,
                mode=color_mode,
            )
            checksum ^= run_checksum
            fps_runs.append(fps)
            ms_runs.append(ms_per_frame)
            gpix_runs.append(gpix_per_s)
            logger.debug(
                "repeat=%d backend=%s mode=%s variant=%s fps=%.2f ms=%.4f gpix/s=%.3f",
                repeat_idx + 1,
                processor_backend,
                color_mode,
                variant,
                fps,
                ms_per_frame,
                gpix_per_s,
            )
    finally:
        if gc_was_enabled:
            gc.enable()

    return BenchResult(
        backend=processor_backend,
        mode=color_mode,
        variant=variant,
        fps=fps_runs,
        ms_per_frame=ms_runs,
        gpix_per_s=gpix_runs,
        checksum=checksum,
    )


def verify_output_parity(
    mode: ColorMode,
    rect: FakeMappedRect,
    width: int,
    height: int,
    region: Region,
    rotation_angle: int,
) -> None:
    cv2_processor = Processor(output_color=mode, backend="cv2")
    numpy_processor = Processor(output_color=mode, backend="numpy")
    frame_cv2 = cv2_processor.process(rect, width, height, region, rotation_angle)
    frame_numpy = numpy_processor.process(rect, width, height, region, rotation_angle)
    if not np.array_equal(frame_cv2, frame_numpy):
        raise RuntimeError(f"Output mismatch for mode={mode} between cv2 and numpy backends.")


def maybe_set_numpy_parallel_threshold(threshold: int | None) -> None:
    if threshold is None:
        return
    kernels = import_module("dxcam.processor._numpy_kernels")
    kernels.set_parallel_pixels_threshold(threshold)
    applied = kernels.get_parallel_pixels_threshold()
    logger.info("Applied numpy parallel threshold: %d pixels", applied)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Micro-benchmark DXcam processor backends (cv2 vs numpy)."
    )
    parser.add_argument("--width", type=int, default=3840)
    parser.add_argument("--height", type=int, default=2160)
    parser.add_argument("--warmup", type=int, default=10)
    parser.add_argument("--iterations", type=int, default=120)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--rotation", type=int, default=0, choices=[0, 90, 180, 270])
    parser.add_argument(
        "--modes",
        nargs="+",
        default=["RGB"],
        choices=["BGRA", "BGR", "RGB", "RGBA", "GRAY"],
    )
    parser.add_argument(
        "--processor-backends",
        nargs="+",
        default=["cv2", "numpy"],
        choices=["cv2", "numpy"],
        help="Backends to include in the benchmark.",
    )
    parser.add_argument(
        "--variants",
        nargs="+",
        default=["process", "into"],
        choices=["process", "into"],
        help="Benchmark allocation path (process), dst-reuse path (into), or both.",
    )
    parser.add_argument(
        "--skip-verify",
        action="store_true",
        help="Skip one-time output parity check between cv2 and numpy backends.",
    )
    parser.add_argument(
        "--numpy-parallel-threshold",
        type=int,
        default=None,
        help=(
            "Override numpy backend OpenMP threshold in pixels. "
            "Use 0 to force parallel path; use a large number to force serial path."
        ),
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    width = args.width
    height = args.height
    region: Region = (0, 0, width, height)
    rect, _raw = make_fake_rect(
        width=width,
        height=height,
        rotation_angle=args.rotation,
    )

    logger.info(
        "Processor micro-benchmark width=%d height=%d rotation=%d warmup=%d iterations=%d repeats=%d pitch=%d",
        width,
        height,
        args.rotation,
        args.warmup,
        args.iterations,
        args.repeats,
        rect.Pitch,
    )

    processor_backends = [
        normalize_processor_backend_name(backend) for backend in args.processor_backends
    ]

    deduped_backends: list[ProcessorBackend] = []
    for backend in processor_backends:
        if backend not in deduped_backends:
            deduped_backends.append(backend)

    if "numpy" in deduped_backends:
        maybe_set_numpy_parallel_threshold(args.numpy_parallel_threshold)

    if not args.skip_verify and "cv2" in deduped_backends and "numpy" in deduped_backends:
        for mode in args.modes:
            verify_output_parity(
                mode=mode,
                rect=rect,
                width=width,
                height=height,
                region=region,
                rotation_angle=args.rotation,
            )
        logger.info("Parity check passed for modes=%s", ",".join(args.modes))

    results: list[BenchResult] = []
    for mode in args.modes:
        for variant in args.variants:
            for processor_backend in deduped_backends:
                result = run_processor_bench(
                    processor_backend=processor_backend,
                    color_mode=mode,
                    variant=variant,
                    rect=rect,
                    width=width,
                    height=height,
                    region=region,
                    rotation_angle=args.rotation,
                    warmup=args.warmup,
                    iterations=args.iterations,
                    repeats=args.repeats,
                )
                results.append(result)
                logger.info(
                    "backend=%s mode=%s variant=%s median_fps=%.2f mean_fps=%.2f median_ms=%.4f median_gpix/s=%.3f checksum=%d",
                    result.backend,
                    result.mode,
                    result.variant,
                    result.median_fps,
                    result.mean_fps,
                    result.median_ms,
                    result.median_gpix_per_s,
                    result.checksum,
                )

    for mode in args.modes:
        for variant in args.variants:
            cv2_result = next(
                (
                    r
                    for r in results
                    if r.backend == "cv2" and r.mode == mode and r.variant == variant
                ),
                None,
            )
            numpy_result = next(
                (
                    r
                    for r in results
                    if r.backend == "numpy" and r.mode == mode and r.variant == variant
                ),
                None,
            )
            if cv2_result is None or numpy_result is None:
                continue
            speedup = numpy_result.median_fps / cv2_result.median_fps
            delta_pct = (speedup - 1.0) * 100.0
            logger.info(
                "comparison mode=%s variant=%s numpy_vs_cv2=%.3fx (%+.2f%%)",
                mode,
                variant,
                speedup,
                delta_pct,
            )


if __name__ == "__main__":
    main()
