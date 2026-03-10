from __future__ import annotations

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")
_numpy_kernels = pytest.importorskip("dxcam.processor._numpy_kernels")


_COLOR_CASES: tuple[tuple[str, int, int], ...] = (
    ("RGB", cv2.COLOR_BGRA2RGB, 3),
    ("BGR", cv2.COLOR_BGRA2BGR, 3),
    ("RGBA", cv2.COLOR_BGRA2RGBA, 4),
    ("GRAY", cv2.COLOR_BGRA2GRAY, 1),
)

_SIZES: tuple[tuple[int, int], ...] = (
    (37, 53),
    (240, 320),
)

_THRESHOLDS: tuple[int, ...] = (
    0,  # force parallel path
    10**9,  # force serial path
)


def _random_bgra(height: int, width: int, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.integers(0, 256, size=(height, width, 4), dtype=np.uint8)


def _cv2_expected(src: np.ndarray, cv2_code: int) -> np.ndarray:
    out = cv2.cvtColor(src, cv2_code)
    if out.ndim == 2:
        return out[..., np.newaxis]
    return out


@pytest.fixture(autouse=True)
def _restore_parallel_threshold() -> None:
    original = _numpy_kernels.get_parallel_pixels_threshold()
    try:
        yield
    finally:
        _numpy_kernels.set_parallel_pixels_threshold(original)


@pytest.mark.parametrize("height,width", _SIZES)
@pytest.mark.parametrize("mode,cv2_code,channels", _COLOR_CASES)
@pytest.mark.parametrize("threshold", _THRESHOLDS)
def test_convert_bgra_matches_cv2(
    height: int,
    width: int,
    mode: str,
    cv2_code: int,
    channels: int,
    threshold: int,
) -> None:
    src = _random_bgra(height=height, width=width, seed=height * 1000 + width)
    _numpy_kernels.set_parallel_pixels_threshold(threshold)

    out = _numpy_kernels.convert_bgra(src, mode)
    expected = _cv2_expected(src, cv2_code)

    assert out.shape == (height, width, channels)
    np.testing.assert_array_equal(out, expected)


@pytest.mark.parametrize("height,width", _SIZES)
@pytest.mark.parametrize("mode,cv2_code,channels", _COLOR_CASES)
@pytest.mark.parametrize("threshold", _THRESHOLDS)
def test_convert_bgra_into_matches_cv2(
    height: int,
    width: int,
    mode: str,
    cv2_code: int,
    channels: int,
    threshold: int,
) -> None:
    src = _random_bgra(height=height, width=width, seed=(height * 1000 + width) ^ 0xA5A5)
    dst = np.empty((height, width, channels), dtype=np.uint8)
    _numpy_kernels.set_parallel_pixels_threshold(threshold)

    _numpy_kernels.convert_bgra_into(src, dst, mode)
    expected = _cv2_expected(src, cv2_code)

    np.testing.assert_array_equal(dst, expected)
