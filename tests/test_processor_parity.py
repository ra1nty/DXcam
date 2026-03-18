from __future__ import annotations

import ctypes
from dataclasses import dataclass
from typing import Any

import numpy as np
import pytest

pytest.importorskip("cv2")

from dxcam.processor import Processor
from dxcam.processor.cython_processor import (
    _CYTHON_KERNELS_AVAILABLE,
    _cython_kernels,
)
from dxcam.processor.cv2_processor import (
    _NUMPY_KERNELS_AVAILABLE,
    _numpy_kernels,
)
from dxcam.types import ProcessorBackend, Region


class FakeMappedRect:
    def __init__(self, pitch: int, pbits: Any) -> None:
        self.Pitch = pitch
        self.pBits = pbits


@dataclass(frozen=True)
class _Case:
    rect: FakeMappedRect
    keepalive: Any
    width: int
    height: int
    region: Region
    rotation: int


_PROCESSOR_BACKENDS: tuple[ProcessorBackend, ...] = ("cython", "numpy")
_COLOR_MODES: tuple[str, ...] = ("RGB", "BGR", "RGBA", "BGRA", "GRAY")
_ROTATIONS: tuple[int, ...] = (0, 90, 180, 270)
_SIZES: tuple[tuple[int, int], ...] = (
    (53, 37),
    (320, 240),
)
_THRESHOLDS: tuple[int, ...] = (
    0,
    10**9,
)


def _aligned_pitch(width_pixels: int, align_bytes: int = 256) -> int:
    row_bytes = width_pixels * 4
    return ((row_bytes + align_bytes - 1) // align_bytes) * align_bytes


def _make_case(
    *,
    width: int,
    height: int,
    rotation: int,
    region: Region,
    seed: int,
) -> _Case:
    rows = height if rotation in (0, 180) else width
    active_cols = width if rotation in (0, 180) else height
    pitch = _aligned_pitch(active_cols)
    size = pitch * rows
    raw = (ctypes.c_ubyte * size)()
    buffer = np.frombuffer(memoryview(raw), dtype=np.uint8)
    rng = np.random.default_rng(seed)
    buffer[:] = rng.integers(0, 256, size=size, dtype=np.uint8)
    rect = FakeMappedRect(
        pitch=pitch,
        pbits=ctypes.cast(raw, ctypes.POINTER(ctypes.c_ubyte)),
    )
    return _Case(
        rect=rect,
        keepalive=raw,
        width=width,
        height=height,
        region=region,
        rotation=rotation,
    )


def _regions_for_size(width: int, height: int) -> tuple[Region, Region]:
    return (
        (0, 0, width, height),
        (3, 5, width - 7, height - 2),
    )


def _channel_count(mode: str) -> int:
    if mode in ("RGB", "BGR"):
        return 3
    if mode == "GRAY":
        return 1
    return 4


@pytest.fixture(autouse=True)
def _restore_parallel_thresholds() -> None:
    cython_original = (
        None
        if _cython_kernels is None
        else _cython_kernels.get_parallel_pixels_threshold()
    )
    numpy_original = (
        None if _numpy_kernels is None else _numpy_kernels.get_parallel_pixels_threshold()
    )
    try:
        yield
    finally:
        if cython_original is not None:
            _cython_kernels.set_parallel_pixels_threshold(cython_original)
        if numpy_original is not None:
            _numpy_kernels.set_parallel_pixels_threshold(numpy_original)


@pytest.mark.parametrize("processor_backend", _PROCESSOR_BACKENDS)
@pytest.mark.parametrize("output_color", _COLOR_MODES)
@pytest.mark.parametrize("rotation", _ROTATIONS)
@pytest.mark.parametrize("width,height", _SIZES)
@pytest.mark.parametrize("threshold", _THRESHOLDS)
def test_processor_parity(
    processor_backend: ProcessorBackend,
    output_color: str,
    rotation: int,
    width: int,
    height: int,
    threshold: int,
) -> None:
    if processor_backend == "cython" and not _CYTHON_KERNELS_AVAILABLE:
        pytest.skip("Cython kernels are unavailable in this environment.")
    if processor_backend == "numpy" and not _NUMPY_KERNELS_AVAILABLE:
        pytest.skip("NumPy kernels are unavailable in this environment.")

    if _cython_kernels is not None:
        _cython_kernels.set_parallel_pixels_threshold(threshold)
    if _numpy_kernels is not None:
        _numpy_kernels.set_parallel_pixels_threshold(threshold)

    reference = Processor(output_color=output_color, backend="cv2")
    candidate = Processor(output_color=output_color, backend=processor_backend)
    channels = _channel_count(output_color)

    for idx, region in enumerate(_regions_for_size(width, height)):
        case = _make_case(
            width=width,
            height=height,
            rotation=rotation,
            region=region,
            seed=(width * 1000 + height * 10 + rotation + idx) ^ threshold,
        )
        expected = reference.process(
            case.rect,
            case.width,
            case.height,
            case.region,
            case.rotation,
        )
        actual = candidate.process(
            case.rect,
            case.width,
            case.height,
            case.region,
            case.rotation,
        )

        out_h = region[3] - region[1]
        out_w = region[2] - region[0]
        assert expected.shape == (out_h, out_w, channels)
        assert actual.shape == expected.shape
        np.testing.assert_array_equal(actual, expected)

        dst = np.empty((out_h, out_w, channels), dtype=np.uint8)
        candidate.process_into(
            case.rect,
            case.width,
            case.height,
            case.region,
            case.rotation,
            dst,
        )
        np.testing.assert_array_equal(dst, expected)
