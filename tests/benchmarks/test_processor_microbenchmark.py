from __future__ import annotations

import ctypes
from dataclasses import dataclass
from typing import Any

import numpy as np
import pytest

pytest.importorskip("pytest_benchmark")
pytest.importorskip("cv2")

from dxcam.processor import Processor
from dxcam.processor.cv2_processor import _NUMPY_KERNELS_AVAILABLE


class FakeMappedRect:
    """Minimal DXGI_MAPPED_RECT-like object for processor microbenchmarks."""

    def __init__(self, pitch: int, pbits: Any) -> None:
        self.Pitch = pitch
        self.pBits = pbits


@dataclass
class _Case:
    rect: FakeMappedRect
    keepalive: Any
    width: int
    height: int
    region: tuple[int, int, int, int]
    rotation: int


def _aligned_pitch(width_pixels: int, align_bytes: int = 256) -> int:
    row_bytes = width_pixels * 4
    return ((row_bytes + align_bytes - 1) // align_bytes) * align_bytes


def _make_case(*, width: int, height: int, rotation: int, seed: int) -> _Case:
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
        region=(0, 0, width, height),
        rotation=rotation,
    )


_COLOR_MODES: tuple[str, ...] = ("RGB", "BGR", "RGBA", "BGRA", "GRAY")
_MODE_CHANNELS: dict[str, int] = {
    "RGB": 3,
    "BGR": 3,
    "RGBA": 4,
    "BGRA": 4,
    "GRAY": 1,
}
_ROTATIONS: tuple[int, ...] = (0, 90, 180, 270)


@pytest.fixture(scope="module")
def cases_by_rotation() -> dict[int, _Case]:
    # 1080p benchmark cases for each supported rotation.
    return {
        rotation: _make_case(width=1920, height=1080, rotation=rotation, seed=rotation + 7)
        for rotation in _ROTATIONS
    }


@pytest.mark.parametrize("processor_backend", ("cv2", "numpy"))
@pytest.mark.parametrize("output_color", _COLOR_MODES)
@pytest.mark.parametrize("rotation", _ROTATIONS)
def test_process_into_microbenchmark_matrix(
    benchmark: Any,
    processor_backend: str,
    output_color: str,
    rotation: int,
    cases_by_rotation: dict[int, _Case],
) -> None:
    if processor_backend == "numpy" and not _NUMPY_KERNELS_AVAILABLE:
        pytest.skip("NumPy/Cython kernels are unavailable in this environment.")

    case = cases_by_rotation[rotation]
    channels = _MODE_CHANNELS[output_color]
    processor = Processor(output_color=output_color, backend=processor_backend)
    dst = np.empty((case.height, case.width, channels), dtype=np.uint8)

    # One warmup call before timing.
    processor.process_into(
        case.rect,
        case.width,
        case.height,
        case.region,
        case.rotation,
        dst,
    )

    benchmark.group = f"process_into_rotate{rotation}"
    benchmark.extra_info["processor_backend"] = processor_backend
    benchmark.extra_info["output_color"] = output_color
    benchmark.extra_info["rotation"] = rotation
    benchmark.extra_info["resolution"] = f"{case.width}x{case.height}"

    def _run() -> int:
        processor.process_into(
            case.rect,
            case.width,
            case.height,
            case.region,
            case.rotation,
            dst,
        )
        return int(dst[0, 0, 0])

    benchmark(_run)
