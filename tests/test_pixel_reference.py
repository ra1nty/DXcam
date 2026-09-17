"""Pixel references independent of DXcam's rotation, crop, and conversion code.

These tests synthesize mapped BGRA memory; they do not assert that either native
capture API actually supplies that memory layout on a particular display.
"""

from __future__ import annotations

from contextlib import contextmanager
from threading import Lock
from types import SimpleNamespace

import numpy as np
import pytest

from dxcam.dxcam import DXCamera
from dxcam.processor import Processor
from dxcam.processor import cv2_processor, cython_processor, numpy_processor


BACKENDS = ("cv2", "numpy", "cython")
COLORS = ("RGB", "BGR", "RGBA", "BGRA", "GRAY")
ROTATIONS = (0, 90, 180, 270)
PAD_PIXEL = (253, 241, 229, 217)
DESTINATION_SENTINEL = 193


def logical_pattern(width=13, height=9):
    """Distinct x/y and channel values, including nonconstant alpha."""
    y, x = np.indices((height, width), dtype=np.int32)
    return np.stack(
        (
            (17 * x + 3 * y + 11) % 256,
            (5 * x + 29 * y + 37) % 256,
            (31 * x + 7 * y + 71) % 256,
            (13 * x + 19 * y + 101) % 256,
        ),
        axis=-1,
    ).astype(np.uint8)


def encode_memory(logical, rotation):
    # DXcam's angle is the clockwise turn needed to display raw DXGI pixels.
    # Generate its inverse with NumPy, without the production coordinate helper.
    return np.ascontiguousarray(np.rot90(logical, k=rotation // 90))


def mapped_memory(memory):
    height, width = memory.shape[:2]
    # Odd padding width catches treating Pitch as the active image width.
    storage = np.empty((height, width + 5, 4), dtype=np.uint8)
    storage[:] = PAD_PIXEL
    storage[:, :width] = memory
    rect = SimpleNamespace(Pitch=storage.strides[0], pBits=storage.ctypes.data)
    return storage, rect


def regions(width, height):
    return (
        (0, 0, width, height),
        (1, 2, width - 2, height - 1),
        (0, height - 2, width, height),
        (width - 1, 0, width, height),
        (width - 1, height - 1, width, height),
    )


def expected_pixels(logical, region, color):
    left, top, right, bottom = region
    cropped = logical[top:bottom, left:right]
    if color != "GRAY":
        # Spell out channel order; alpha is preserved, not reconstructed.
        order = {
            "BGRA": [0, 1, 2, 3],
            "RGBA": [2, 1, 0, 3],
            "BGR": [0, 1, 2],
            "RGB": [2, 1, 0],
        }[color]
        return cropped[..., order].copy()

    # OpenCV documents Y = .299 R + .587 G + .114 B. Its uint8 reference uses
    # 15-bit coefficients and nearest rounding (ties upward), not truncation
    # or np.rint's ties-to-even. Derive the coefficients from those decimal
    # weights instead of importing any DXcam/OpenCV conversion implementation.
    # https://docs.opencv.org/4.13.0/de/d25/imgproc_color_conversions.html
    # https://github.com/opencv/opencv/blob/4.13.0/modules/imgproc/src/color.simd_helpers.hpp
    # https://github.com/opencv/opencv/blob/4.13.0/modules/imgproc/src/color_rgb.simd.hpp
    scale = 1 << 15
    weights = (np.array([114, 587, 299], dtype=np.int64) * scale + 500) // 1000
    weighted = np.sum(cropped[..., :3].astype(np.int64) * weights, axis=-1)
    return ((weighted + scale // 2) // scale).astype(np.uint8)[..., None]


def make_processor(backend, color, *, require_compiled=True):
    if backend == "cv2":
        pytest.importorskip("cv2")
    elif backend == "numpy":
        if require_compiled and not numpy_processor._NUMPY_KERNELS_AVAILABLE:
            pytest.skip("Compiled NumPy processor is unavailable")
        if not numpy_processor._NUMPY_KERNELS_AVAILABLE:
            pytest.importorskip("cv2")
    elif not cython_processor._CYTHON_KERNELS_AVAILABLE:
        pytest.skip("Direct Cython processor is unavailable")
    return Processor(backend=backend, output_color=color)


def assert_process_outputs(candidate, rect, logical, region, rotation, color):
    height, width = logical.shape[:2]
    expected = expected_pixels(logical, region, color)
    actual = candidate.process(rect, width, height, region, rotation)
    np.testing.assert_array_equal(actual, expected)
    assert actual.dtype == np.uint8

    for strided in (False, True):
        out_h, out_w, channels = expected.shape
        if strided:
            # Positive, padded row stride is supported by every backend. Pixel
            # and channel strides need not be supported by OpenCV's dst API.
            storage = np.full(
                (out_h * 2 + 1, out_w + 2, channels),
                DESTINATION_SENTINEL,
                dtype=np.uint8,
            )
            dst = storage[1::2, 1:-1]
        else:
            storage = np.full(expected.shape, DESTINATION_SENTINEL, dtype=np.uint8)
            dst = storage
        candidate.process_into(rect, width, height, region, rotation, dst)
        np.testing.assert_array_equal(dst, expected)
        if strided:
            np.testing.assert_array_equal(storage[::2], DESTINATION_SENTINEL)
            np.testing.assert_array_equal(storage[:, 0], DESTINATION_SENTINEL)
            np.testing.assert_array_equal(storage[:, -1], DESTINATION_SENTINEL)


@pytest.mark.parametrize(
    "rotation,expected_ids",
    (
        (0, [[1, 2, 3], [4, 5, 6]]),
        (90, [[3, 6], [2, 5], [1, 4]]),
        (180, [[6, 5, 4], [3, 2, 1]]),
        (270, [[4, 1], [5, 2], [6, 3]]),
    ),
)
def test_memory_rotation_fixture_has_manually_anchored_orientation(
    rotation, expected_ids
):
    # Labels explicitly fix all corners and orientation, independently of the
    # rot90 call used to synthesize memory for the larger coordinate patterns.
    logical = np.zeros((2, 3, 4), dtype=np.uint8)
    logical[..., 0] = [[1, 2, 3], [4, 5, 6]]
    np.testing.assert_array_equal(
        encode_memory(logical, rotation)[..., 0], expected_ids
    )


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("color", COLORS)
@pytest.mark.parametrize("rotation", ROTATIONS)
def test_processor_pixels_match_independent_logical_reference(backend, color, rotation):
    logical = logical_pattern()
    source, rect = mapped_memory(encode_memory(logical, rotation))
    before = source.copy()
    candidate = make_processor(backend, color)
    for region in regions(logical.shape[1], logical.shape[0]):
        assert_process_outputs(candidate, rect, logical, region, rotation, color)
    np.testing.assert_array_equal(source, before)


@pytest.mark.parametrize("backend", BACKENDS)
def test_gray_rounding_and_alpha_independence_match_explicit_reference(backend):
    # Primaries/black/white, values around a rounding boundary, a 15-bit exact
    # tie, and decimal-vs-fixed-point boundary cases. Expected bytes are literal;
    # a blanket +/-1 allowance would hide failures these samples distinguish.
    bgr = np.array(
        [
            [0, 0, 0],
            [255, 255, 255],
            [255, 0, 0],
            [0, 255, 0],
            [0, 0, 255],
            [0, 0, 1],
            [0, 0, 2],
            [0, 0, 5],
            [0, 0, 6],
            [17, 9, 21],
            [0, 2, 175],
            [0, 5, 236],
        ],
        dtype=np.uint8,
    )
    expected_row = [0, 255, 29, 150, 76, 0, 1, 1, 2, 14, 54, 74]
    logical = np.empty((2, len(bgr), 4), dtype=np.uint8)
    logical[..., :3] = bgr
    logical[0, :, 3] = 0
    logical[1, :, 3] = 255
    expected = np.array([expected_row, expected_row], dtype=np.uint8)[..., None]
    region = (0, 0, len(bgr), 2)
    np.testing.assert_array_equal(expected_pixels(logical, region, "GRAY"), expected)
    source, rect = mapped_memory(logical)
    candidate = make_processor(backend, "GRAY")
    np.testing.assert_array_equal(
        candidate.process(rect, len(bgr), 2, region, 0), expected
    )
    assert source.ctypes.data == rect.pBits


def disable_preparation_extension(monkeypatch):
    monkeypatch.setattr(cv2_processor, "_NUMPY_KERNELS_AVAILABLE", False)
    monkeypatch.setattr(cv2_processor, "_numpy_kernels", None)


@pytest.mark.parametrize("backend", ("cv2", "numpy"))
@pytest.mark.parametrize("rotation", ROTATIONS)
def test_extension_fallback_matches_logical_reference(monkeypatch, backend, rotation):
    disable_preparation_extension(monkeypatch)
    monkeypatch.setattr(numpy_processor, "_NUMPY_KERNELS_AVAILABLE", False)
    monkeypatch.setattr(numpy_processor, "_numpy_kernels", None)
    monkeypatch.setattr(
        numpy_processor.NumpyProcessor, "_missing_extension_warned", False
    )
    logical = logical_pattern(width=9, height=13)
    source, rect = mapped_memory(encode_memory(logical, rotation))
    before = source.copy()
    for color in COLORS:
        candidate = make_processor(backend, color, require_compiled=False)
        for region in ((0, 0, 9, 13), (2, 1, 8, 10)):
            assert_process_outputs(candidate, rect, logical, region, rotation, color)
    np.testing.assert_array_equal(source, before)


@pytest.mark.parametrize("rotation", ROTATIONS)
def test_bgra_rotation_fallback_without_opencv_matches_logical_reference(
    monkeypatch, rotation
):
    disable_preparation_extension(monkeypatch)
    monkeypatch.setattr(
        cv2_processor.Cv2Processor, "_get_cv2_rotate_module", lambda self: None
    )
    logical = logical_pattern()
    source, rect = mapped_memory(encode_memory(logical, rotation))
    before = source.copy()
    candidate = Processor(backend="cv2", output_color="BGRA")
    for region in regions(13, 9):
        assert_process_outputs(candidate, rect, logical, region, rotation, "BGRA")
    np.testing.assert_array_equal(source, before)


class ArrayCopyStage:
    """Model the D3D box copy literally, then expose a padded mapped surface."""

    def ensure_size(self, *, dim):
        self.dim = dim

    def copy_region_from(self, *, im_context, src_texture, src_region):
        left, top, right, bottom = src_region
        assert 0 <= left < right <= src_texture.shape[1]
        assert 0 <= top < bottom <= src_texture.shape[0]
        assert self.dim == (right - left, bottom - top)
        self.storage, self.rect = mapped_memory(src_texture[top:bottom, left:right])

    @contextmanager
    def mapped(self):
        yield self.rect


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("rotation", ROTATIONS)
def test_gpu_roi_copy_then_readout_matches_logical_crop(backend, rotation):
    # Exercise production GPU ROI coordinate selection followed by real
    # processing. The expected crop never calls resolve_capture_copy_spec.
    logical = logical_pattern(width=9, height=13)
    memory = encode_memory(logical, rotation)
    camera = DXCamera.__new__(DXCamera)
    camera._is_released = True  # This synthetic instance owns no native resources.
    camera.backend = "dxgi"
    camera.rotation_angle = rotation
    camera.channel_size = 4
    camera._DXCamera__processor_lock = Lock()
    camera._processor = make_processor(backend, "BGRA")
    camera._output = SimpleNamespace(surface_size=(memory.shape[1], memory.shape[0]))
    camera._device = SimpleNamespace(im_context=object())
    camera._duplicator = SimpleNamespace(texture=memory)

    for region in regions(9, 13):
        stage = ArrayCopyStage()
        width, height = camera._copy_region_to_surface(region, stage)
        assert (width, height) == (region[2] - region[0], region[3] - region[1])
        frame = camera._process_stage(
            stage=stage,
            frame_width=width,
            frame_height=height,
            rotation_angle=rotation,
        )
        np.testing.assert_array_equal(frame, expected_pixels(logical, region, "BGRA"))
