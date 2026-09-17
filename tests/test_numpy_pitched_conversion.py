"""Exact conversion contracts for mapped BGRA rows and unusual array layouts."""

from __future__ import annotations

import numpy as np
import pytest

from test_pixel_reference import expected_pixels, logical_pattern


COLORS = ("RGB", "BGR", "RGBA", "GRAY")


@pytest.fixture(params=[10**9, 0], ids=["serial", "parallel"])
def kernels(request):
    module = pytest.importorskip("dxcam.processor._numpy_kernels")
    previous = module.get_parallel_pixels_threshold()
    module.set_parallel_pixels_threshold(request.param)
    try:
        yield module
    finally:
        module.set_parallel_pixels_threshold(previous)


def reference(source, color):
    height, width = source.shape[:2]
    return expected_pixels(source, (0, 0, width, height), color)


def assert_conversion(kernels, source, color):
    expected = reference(source, color)
    actual = kernels.convert_bgra(source, color)
    assert actual.dtype == np.uint8
    assert actual.flags.c_contiguous
    assert not np.shares_memory(actual, source)
    np.testing.assert_array_equal(actual, expected)
    destination = np.full(expected.shape, 193, dtype=np.uint8)
    kernels.convert_bgra_into(source, destination, color)
    np.testing.assert_array_equal(destination, expected)


def positive_source(layout):
    if layout == "odd-byte-pitch":
        height, width = 9, 13
        pitch = width * 4 + 7
        storage = np.full(3 + height * pitch, 229, dtype=np.uint8)
        source = np.ndarray(
            (height, width, 4),
            dtype=np.uint8,
            buffer=storage,
            offset=3,
            strides=(pitch, 4, 1),
        )
        source[:] = logical_pattern(width, height)
        return storage, source
    storage = logical_pattern(23, 17)
    if layout == "padded":
        source = storage[:, :13]
    elif layout == "cropped":
        source = storage[2:13, 3:18]
    else:
        assert layout == "skipped-rows"
        source = storage[1::2, 2:15]
    return storage, source


@pytest.mark.parametrize(
    "layout", ["padded", "cropped", "skipped-rows", "odd-byte-pitch"]
)
@pytest.mark.parametrize("color", COLORS)
def test_positive_pitched_sources_preserve_exact_pixels(kernels, layout, color):
    storage, source = positive_source(layout)
    before = storage.copy()
    assert not source.flags.c_contiguous
    # Both writable and read-only sources are legal. No conversion may alter
    # active pixels, skipped rows, or the padding around the source view.
    for writable in (True, False):
        source.flags.writeable = writable
        assert_conversion(kernels, source, color)
        np.testing.assert_array_equal(storage, before)


def unusual_source(layout):
    storage = logical_pattern(17, 11)
    if layout == "negative-rows":
        return storage, storage[::-1]
    if layout == "negative-pixels":
        return storage, storage[:, ::-1]
    if layout == "skipped-pixels":
        return storage, storage[:, ::2]
    if layout == "negative-channels":
        return storage, storage[..., ::-1]
    if layout == "skipped-channels":
        expanded = np.full((11, 17, 8), 251, dtype=np.uint8)
        expanded[..., ::2] = storage
        return expanded, expanded[..., ::2]
    if layout == "transposed-pixels":
        return storage, storage.transpose(1, 0, 2)
    assert layout == "broadcast-rows"
    return storage, np.broadcast_to(storage[:1], storage.shape)


@pytest.mark.parametrize(
    "layout",
    [
        "negative-rows",
        "negative-pixels",
        "skipped-pixels",
        "negative-channels",
        "skipped-channels",
        "transposed-pixels",
        "broadcast-rows",
    ],
)
def test_unsupported_strides_keep_copy_fallback_semantics(kernels, layout):
    storage, source = unusual_source(layout)
    before = storage.copy()
    for color in COLORS:
        assert_conversion(kernels, source, color)
    np.testing.assert_array_equal(storage, before)


@pytest.mark.parametrize("color", COLORS)
def test_overlapping_pitched_source_is_read_as_a_snapshot(kernels, color):
    height, width = 7, 11
    pitch = (width + 3) * 4
    storage = np.full(height * pitch * 2, 211, dtype=np.uint8)
    source = np.ndarray(
        (height, width, 4), dtype=np.uint8, buffer=storage, strides=(pitch, 4, 1)
    )
    source[:] = logical_pattern(width, height)
    expected = reference(source, color)
    # A forward offset overwrites bytes in the next unread source row. A row-
    # by-row conversion without first snapshotting the source corrupts pixels.
    offset = pitch + 3
    destination = np.ndarray(
        expected.shape, dtype=np.uint8, buffer=storage, offset=offset
    )
    assert np.shares_memory(source, destination)
    assert not source.flags.c_contiguous
    assert destination.flags.c_contiguous
    before = storage.copy()

    kernels.convert_bgra_into(source, destination, color)

    np.testing.assert_array_equal(destination, expected)
    np.testing.assert_array_equal(storage[:offset], before[:offset])
    end = offset + destination.nbytes
    np.testing.assert_array_equal(storage[end:], before[end:])


def test_pitched_source_does_not_relax_destination_requirements(kernels):
    _, source = positive_source("cropped")
    for color in COLORS:
        shape = reference(source, color).shape
        for readonly, strided, error in (
            (True, False, "must be writable"),
            (True, True, "must be writable"),
            (False, True, "must be C-contiguous"),
        ):
            height, width, channels = shape
            storage = np.full((height, width * 2, channels), 193, np.uint8)
            destination = storage[:, ::2] if strided else np.full(shape, 193, np.uint8)
            destination.flags.writeable = not readonly
            before = storage.copy()
            with pytest.raises(ValueError, match=error):
                kernels.convert_bgra_into(source, destination, color)
            np.testing.assert_array_equal(destination, 193)
            np.testing.assert_array_equal(storage, before)


def test_empty_and_single_row_layouts_keep_shapes_and_pixels(kernels):
    for height, width in [(0, 13), (9, 0), (0, 0), (1, 13), (1, 1), (9, 1)]:
        storage = np.full((height, width + 5, 4), 229, dtype=np.uint8)
        source = storage[:, :width]
        source[:] = logical_pattern(width, height)
        before = storage.copy()
        source.flags.writeable = False
        for color in COLORS:
            assert_conversion(kernels, source, color)
        np.testing.assert_array_equal(storage, before)
