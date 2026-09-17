from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import numpy as np
import pytest

from dxcam.dxcam import DXCamera
from dxcam.processor import Processor
from dxcam.processor.cython_processor import _CYTHON_KERNELS_AVAILABLE
from dxcam.processor.cv2_processor import _NUMPY_KERNELS_AVAILABLE, _numpy_kernels
from dxcam.util.frame import validate_destination_frame


COLORS = ("RGB", "BGR", "RGBA", "BGRA", "GRAY")
ROTATIONS = (0, 90, 180, 270)
WIDTH, HEIGHT = 3, 2
REGION = (0, 0, WIDTH, HEIGHT)


def channels(color):
    return 1 if color == "GRAY" else len(color)


def source(rotation):
    rows, columns = (HEIGHT, WIDTH) if rotation in (0, 180) else (WIDTH, HEIGHT)
    # One padding pixel in every row must stay outside the active image.
    pixels = np.arange(rows * (columns + 1) * 4, dtype=np.uint8).reshape(
        rows, columns + 1, 4
    )
    rect = SimpleNamespace(Pitch=pixels.strides[0], pBits=pixels.ctypes.data)
    return pixels, rect


def destination(color, strided):
    storage = np.full(
        (HEIGHT * (2 if strided else 1), WIDTH, channels(color)),
        211,
        dtype=np.uint8,
    )
    return storage, storage[::2] if strided else storage


def processor(backend, color):
    if backend == "numpy" and not _NUMPY_KERNELS_AVAILABLE:
        pytest.skip("NumPy kernels are unavailable")
    if backend == "cython" and not _CYTHON_KERNELS_AVAILABLE:
        pytest.skip("Direct Cython kernels are unavailable")
    if backend == "cv2":
        cv2 = pytest.importorskip("cv2")
        errors = (ValueError, cv2.error)
    else:
        errors = (ValueError,)
    return Processor(backend=backend, output_color=color), errors


@pytest.mark.parametrize("backend", ("cv2", "numpy", "cython"))
@pytest.mark.parametrize("color", COLORS)
@pytest.mark.parametrize("rotation", ROTATIONS)
@pytest.mark.parametrize("strided", (False, True))
def test_processors_reject_readonly_outputs_without_changing_storage(
    backend, color, rotation, strided
):
    candidate, errors = processor(backend, color)
    pixels, rect = source(rotation)
    storage, dst = destination(color, strided)
    before = storage.copy()
    dst.flags.writeable = False
    with pytest.raises(errors, match="(?i)(read.?only|writ)"):
        candidate.process_into(rect, WIDTH, HEIGHT, REGION, rotation, dst)
    np.testing.assert_array_equal(storage, before)
    assert pixels.ctypes.data == rect.pBits  # Keep mapped source storage alive.


@pytest.mark.parametrize("backend", ("cv2", "numpy", "cython"))
@pytest.mark.parametrize("color", COLORS)
@pytest.mark.parametrize("rotation", ROTATIONS)
def test_writable_row_strided_outputs_remain_supported(backend, color, rotation):
    candidate, _ = processor(backend, color)
    pixels, rect = source(rotation)
    expected = np.empty((HEIGHT, WIDTH, channels(color)), dtype=np.uint8)
    candidate.process_into(rect, WIDTH, HEIGHT, REGION, rotation, expected)
    storage, dst = destination(color, strided=True)
    assert not dst.flags.c_contiguous
    validate_destination_frame(
        dst, frame_width=WIDTH, frame_height=HEIGHT, channel_size=channels(color)
    )
    candidate.process_into(rect, WIDTH, HEIGHT, REGION, rotation, dst)
    np.testing.assert_array_equal(dst, expected)
    assert np.all(storage[1::2] == 211)
    assert pixels.ctypes.data == rect.pBits


@pytest.fixture
def numpy_kernels():
    if not _NUMPY_KERNELS_AVAILABLE:
        pytest.skip("NumPy kernels are unavailable")
    return _numpy_kernels


@pytest.mark.parametrize("color", ("RGB", "BGR", "RGBA", "GRAY"))
@pytest.mark.parametrize("strided", (False, True))
def test_direct_numpy_conversion_rejects_readonly_destination(
    numpy_kernels, color, strided
):
    pixels = np.arange(HEIGHT * WIDTH * 4, dtype=np.uint8).reshape(HEIGHT, WIDTH, 4)
    storage, dst = destination(color, strided)
    before = storage.copy()
    dst.flags.writeable = False
    with pytest.raises(ValueError, match="must be writable"):
        numpy_kernels.convert_bgra_into(pixels, dst, color)
    np.testing.assert_array_equal(storage, before)


@pytest.mark.parametrize("rotation", ROTATIONS)
@pytest.mark.parametrize("strided", (False, True))
def test_direct_numpy_preparation_rejects_readonly_destination(
    numpy_kernels, rotation, strided
):
    pixels, _ = source(rotation)
    storage, dst = destination("BGRA", strided)
    before = storage.copy()
    dst.flags.writeable = False
    with pytest.raises(ValueError, match="must be writable"):
        numpy_kernels.prepare_bgra_into(pixels, dst, WIDTH, HEIGHT, REGION, rotation)
    np.testing.assert_array_equal(storage, before)


@pytest.mark.parametrize("color", COLORS)
@pytest.mark.parametrize("strided", (False, True))
def test_public_destination_validator_requires_writable_storage(color, strided):
    storage, dst = destination(color, strided)
    before = storage.copy()
    dst.flags.writeable = False
    with pytest.raises(ValueError, match="must be writable"):
        validate_destination_frame(
            dst, frame_width=WIDTH, frame_height=HEIGHT, channel_size=channels(color)
        )
    np.testing.assert_array_equal(storage, before)


@pytest.mark.parametrize("path", ("one_shot", "cached", "threaded_grab", "latest"))
@pytest.mark.parametrize("strided", (False, True))
def test_camera_into_paths_reject_readonly_before_processing(path, strided):
    camera = DXCamera.__new__(DXCamera)
    camera._is_released = False
    camera.is_capturing = path in ("threaded_grab", "latest")
    camera.region = REGION
    camera.rotation_angle = 0
    camera.channel_size = 3
    camera._stagesurf = object()
    camera._capture_to_stage = lambda *args, **kwargs: (
        path != "cached",
        100,
        WIDTH,
        HEIGHT,
        0,
    )
    camera._DXCamera__last_grab_entry = (
        REGION,
        np.zeros((HEIGHT, WIDTH, 3), dtype=np.uint8),
    )
    camera._process_stage = lambda **kwargs: pytest.fail(
        "readonly destination reached processing"
    )

    @contextmanager
    def read_lease(**kwargs):
        yield SimpleNamespace(
            stage=camera._stagesurf,
            frame_width=WIDTH,
            frame_height=HEIGHT,
            rotation_angle=0,
        )

    camera._read_lease = read_lease
    storage, dst = destination("BGR", strided)
    before = storage.copy()
    dst.flags.writeable = False
    try:
        with pytest.raises(ValueError, match="must be writable"):
            if path == "latest":
                camera.get_latest_frame_into(dst)
            else:
                camera.grab_into(dst, new_frame_only=path != "cached")
    finally:
        # This synthetic camera never owns native resources.
        camera._is_released = True
    np.testing.assert_array_equal(storage, before)
