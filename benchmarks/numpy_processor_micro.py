from __future__ import annotations

import argparse
import ctypes
import logging
import time
from importlib import import_module
from typing import Any

import numpy as np

from dxcam.processor.numpy_processor import NumpyProcessor
from dxcam.types import Region

logger = logging.getLogger(__name__)


class FakeMappedRect:
    """Minimal DXGI_MAPPED_RECT-like object for processor micro-benchmarking."""

    def __init__(self, pitch: int, pbits: ctypes.POINTER(ctypes.c_ubyte)) -> None:
        self.Pitch = pitch
        self.pBits = pbits


def aligned_pitch(width: int, align_bytes: int = 256) -> int:
    row_bytes = width * 4
    return ((row_bytes + align_bytes - 1) // align_bytes) * align_bytes


def make_fake_rect(width: int, height: int) -> tuple[FakeMappedRect, Any]:
    pitch = aligned_pitch(width)
    size = pitch * height
    raw = (ctypes.c_ubyte * size)()
    buffer = np.frombuffer(raw, dtype=np.uint8)
    rng = np.random.default_rng(42)
    buffer[:] = rng.integers(0, 256, size=size, dtype=np.uint8)
    return FakeMappedRect(pitch=pitch, pbits=ctypes.cast(raw, ctypes.POINTER(ctypes.c_ubyte))), raw


def run_processor_bench(
    color_mode: str,
    rect: FakeMappedRect,
    width: int,
    height: int,
    region: Region,
    rotation_angle: int,
    warmup: int,
    iterations: int,
) -> tuple[float, float]:
    processor = NumpyProcessor(color_mode=color_mode)  # type: ignore[arg-type]
    checksum = 0
    for _ in range(warmup):
        frame = processor.process(rect, width, height, region, rotation_angle)
        checksum ^= int(frame[0, 0, 0])

    start = time.perf_counter()
    for _ in range(iterations):
        frame = processor.process(rect, width, height, region, rotation_angle)
        checksum ^= int(frame[0, 0, 0])
    elapsed = time.perf_counter() - start
    fps = iterations / elapsed
    ms_per_frame = (elapsed / iterations) * 1000
    # Keep checksum in output to prevent dead-code elimination style regressions.
    logger.debug("mode=%s checksum=%d", color_mode, checksum)
    return fps, ms_per_frame


def run_bgr_cv2_baseline(
    rect: FakeMappedRect,
    width: int,
    height: int,
    warmup: int,
    iterations: int,
) -> tuple[float, float]:
    cv2 = import_module("cv2")
    pbyte = ctypes.POINTER(ctypes.c_ubyte)
    pitch = int(rect.Pitch)
    size = pitch * height
    pitch_pixels = pitch // 4

    checksum = 0
    for _ in range(warmup):
        mapped = ctypes.cast(rect.pBits, pbyte)
        image = np.ctypeslib.as_array(mapped, shape=(size,)).reshape((height, pitch_pixels, 4))
        out = cv2.cvtColor(image[:, :width, :], cv2.COLOR_BGRA2BGR)
        checksum ^= int(out[0, 0, 0])

    start = time.perf_counter()
    for _ in range(iterations):
        mapped = ctypes.cast(rect.pBits, pbyte)
        image = np.ctypeslib.as_array(mapped, shape=(size,)).reshape((height, pitch_pixels, 4))
        out = cv2.cvtColor(image[:, :width, :], cv2.COLOR_BGRA2BGR)
        checksum ^= int(out[0, 0, 0])
    elapsed = time.perf_counter() - start
    fps = iterations / elapsed
    ms_per_frame = (elapsed / iterations) * 1000
    logger.debug("baseline=bgr_cv2 checksum=%d", checksum)
    return fps, ms_per_frame


def run_rgb_cv2_baselines(
    rect: FakeMappedRect,
    width: int,
    height: int,
    warmup: int,
    iterations: int,
) -> tuple[tuple[float, float], tuple[float, float]]:
    cv2 = import_module("cv2")
    pbyte = ctypes.POINTER(ctypes.c_ubyte)
    pitch = int(rect.Pitch)
    size = pitch * height
    pitch_pixels = pitch // 4
    dst = np.empty((height, width, 3), dtype=np.uint8)

    checksum_alloc = 0
    for _ in range(warmup):
        mapped = ctypes.cast(rect.pBits, pbyte)
        image = np.ctypeslib.as_array(mapped, shape=(size,)).reshape((height, pitch_pixels, 4))
        out = cv2.cvtColor(image[:, :width, :], cv2.COLOR_BGRA2RGB)
        checksum_alloc ^= int(out[0, 0, 0])

    start = time.perf_counter()
    for _ in range(iterations):
        mapped = ctypes.cast(rect.pBits, pbyte)
        image = np.ctypeslib.as_array(mapped, shape=(size,)).reshape((height, pitch_pixels, 4))
        out = cv2.cvtColor(image[:, :width, :], cv2.COLOR_BGRA2RGB)
        checksum_alloc ^= int(out[0, 0, 0])
    elapsed = time.perf_counter() - start
    alloc_metrics = (iterations / elapsed, (elapsed / iterations) * 1000)

    checksum_reuse = 0
    for _ in range(warmup):
        mapped = ctypes.cast(rect.pBits, pbyte)
        image = np.ctypeslib.as_array(mapped, shape=(size,)).reshape((height, pitch_pixels, 4))
        cv2.cvtColor(image[:, :width, :], cv2.COLOR_BGRA2RGB, dst=dst)
        checksum_reuse ^= int(dst[0, 0, 0])

    start = time.perf_counter()
    for _ in range(iterations):
        mapped = ctypes.cast(rect.pBits, pbyte)
        image = np.ctypeslib.as_array(mapped, shape=(size,)).reshape((height, pitch_pixels, 4))
        cv2.cvtColor(image[:, :width, :], cv2.COLOR_BGRA2RGB, dst=dst)
        checksum_reuse ^= int(dst[0, 0, 0])
    elapsed = time.perf_counter() - start
    reuse_metrics = (iterations / elapsed, (elapsed / iterations) * 1000)

    logger.debug("baseline=rgb_cv2_alloc checksum=%d", checksum_alloc)
    logger.debug("baseline=rgb_cv2_reuse checksum=%d", checksum_reuse)
    return alloc_metrics, reuse_metrics


def main() -> None:
    parser = argparse.ArgumentParser(description="Micro-benchmark dxcam NumpyProcessor.")
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument("--warmup", type=int, default=100)
    parser.add_argument("--iterations", type=int, default=1000)
    parser.add_argument("--rotation", type=int, default=0, choices=[0, 90, 180, 270])
    parser.add_argument(
        "--modes",
        nargs="+",
        default=["BGRA", "BGR", "RGB", "GRAY"],
        choices=["BGRA", "BGR", "RGB", "RGBA", "GRAY"],
    )
    parser.add_argument(
        "--compare-bgr-cv2",
        action="store_true",
        help="Also benchmark BGR using cv2.cvtColor baseline for comparison.",
    )
    parser.add_argument(
        "--compare-rgb-cv2",
        action="store_true",
        help="Also benchmark RGB cv2 allocation vs dst-reuse baselines.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    width = args.width
    height = args.height
    region: Region = (0, 0, width, height)
    rect, _raw = make_fake_rect(width=width, height=height)

    logger.info(
        "NumpyProcessor micro-benchmark width=%d height=%d rotation=%d warmup=%d iterations=%d pitch=%d",
        width,
        height,
        args.rotation,
        args.warmup,
        args.iterations,
        rect.Pitch,
    )

    for mode in args.modes:
        fps, ms_per_frame = run_processor_bench(
            color_mode=mode,
            rect=rect,
            width=width,
            height=height,
            region=region,
            rotation_angle=args.rotation,
            warmup=args.warmup,
            iterations=args.iterations,
        )
        logger.info(
            "mode=%s fps=%.2f ms_per_frame=%.4f",
            mode,
            fps,
            ms_per_frame,
        )

    if args.compare_bgr_cv2:
        fps, ms_per_frame = run_bgr_cv2_baseline(
            rect=rect,
            width=width,
            height=height,
            warmup=args.warmup,
            iterations=args.iterations,
        )
        logger.info(
            "baseline=bgr_cv2 fps=%.2f ms_per_frame=%.4f",
            fps,
            ms_per_frame,
        )

    if args.compare_rgb_cv2:
        alloc_metrics, reuse_metrics = run_rgb_cv2_baselines(
            rect=rect,
            width=width,
            height=height,
            warmup=args.warmup,
            iterations=args.iterations,
        )
        logger.info(
            "baseline=rgb_cv2_alloc fps=%.2f ms_per_frame=%.4f",
            alloc_metrics[0],
            alloc_metrics[1],
        )
        logger.info(
            "baseline=rgb_cv2_reuse fps=%.2f ms_per_frame=%.4f",
            reuse_metrics[0],
            reuse_metrics[1],
        )


if __name__ == "__main__":
    main()
