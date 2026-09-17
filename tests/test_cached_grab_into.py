from __future__ import annotations

import numpy as np
import pytest

from dxcam.dxcam import DXCamera


@pytest.fixture
def camera():
    result = DXCamera.__new__(DXCamera)
    result._is_released = False
    result.is_capturing = False
    result.width, result.height = 5, 3
    result.channel_size = 3
    result.region = (0, 0, result.width, result.height)
    result.rotation_angle = 0
    result._stagesurf = object()
    result._DXCamera__last_grab_entry = None
    result._capture_to_stage = lambda *args, **kwargs: (False, 0, 0, 0, 0)
    yield result
    result._is_released = True


def pixels(camera):
    return np.arange(
        camera.width * camera.height * camera.channel_size, dtype=np.uint8
    ).reshape(camera.height, camera.width, camera.channel_size)


def test_cached_into_does_not_allocate_an_intermediate_array(camera, monkeypatch):
    cached = pixels(camera)
    camera._set_cached_grab_frame(camera.region, cached)
    dst = np.empty_like(cached)
    arrays = []
    original_array = np.array

    def tracked_array(*args, **kwargs):
        arrays.append((args, kwargs))
        return original_array(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(np, "array", tracked_array)
        assert camera.grab_into(dst, new_frame_only=False) is True

    assert not arrays
    np.testing.assert_array_equal(dst, cached)
    dst.fill(255)
    np.testing.assert_array_equal(camera._DXCamera__last_grab_entry[1], pixels(camera))


@pytest.mark.parametrize("layout", ["rows", "columns", "reversed"])
def test_cached_into_supports_strided_destinations_without_touching_gaps(
    camera, layout
):
    cached = pixels(camera)
    camera._set_cached_grab_frame(camera.region, cached)
    if layout == "rows":
        storage = np.full((camera.height * 2, camera.width, 3), 251, dtype=np.uint8)
        dst = storage[::2]
    elif layout == "columns":
        storage = np.full((camera.height, camera.width * 2, 3), 251, dtype=np.uint8)
        dst = storage[:, ::2]
    else:
        storage = np.full_like(cached, 251)
        dst = storage[::-1, ::-1]
    assert not dst.flags.c_contiguous

    assert camera.grab_into(dst, new_frame_only=False) is True

    np.testing.assert_array_equal(dst, cached)
    if layout == "rows":
        assert np.all(storage[1::2] == 251)
    elif layout == "columns":
        assert np.all(storage[:, 1::2] == 251)


@pytest.mark.parametrize("invalid", ["readonly", "shape", "dtype"])
def test_cached_into_rejects_invalid_destination_without_writing(camera, invalid):
    cached = pixels(camera)
    camera._set_cached_grab_frame(camera.region, cached)
    if invalid == "shape":
        dst = np.full((camera.height, camera.width + 1, 3), 251, dtype=np.uint8)
        message = "shape mismatch"
    elif invalid == "dtype":
        dst = np.full(cached.shape, 251, dtype=np.float32)
        message = "dtype mismatch"
    else:
        dst = np.full_like(cached, 251)
        dst.flags.writeable = False
        message = "writable"
    before = dst.copy()

    with pytest.raises(ValueError, match=message):
        camera.grab_into(dst, new_frame_only=False)

    np.testing.assert_array_equal(dst, before)
    np.testing.assert_array_equal(camera._DXCamera__last_grab_entry[1], pixels(camera))


@pytest.mark.parametrize("miss", ["empty", "region", "new_frame_only"])
def test_no_matching_frame_preserves_destination_without_validating_it(camera, miss):
    if miss != "empty":
        cache_region = camera.region if miss == "new_frame_only" else (1, 0, 5, 3)
        camera._set_cached_grab_frame(cache_region, pixels(camera))
    # Validation must stay deferred until a frame is available to write.
    dst = np.full((1, 1, 1), 251, dtype=np.float32)
    dst.flags.writeable = False

    assert camera.grab_into(dst, new_frame_only=miss == "new_frame_only") is False
    assert np.all(dst == 251)


def test_fresh_into_cache_keeps_independent_storage(camera):
    expected = pixels(camera)
    captures = iter([(True, 1, camera.width, camera.height, 0), (False, 0, 0, 0, 0)])
    camera._capture_to_stage = lambda *args, **kwargs: next(captures)

    def process(*, dst, **kwargs):
        dst[...] = expected
        return dst

    camera._process_stage = process
    dst = np.empty_like(expected)
    assert camera.grab_into(dst, new_frame_only=False) is True
    cached = camera._DXCamera__last_grab_entry[1]
    assert not np.shares_memory(cached, dst)
    dst.fill(255)

    assert camera.grab_into(dst, new_frame_only=False) is True
    np.testing.assert_array_equal(dst, expected)
    np.testing.assert_array_equal(cached, expected)


def test_cached_grab_results_remain_independently_owned(camera):
    cached = pixels(camera)
    camera._set_cached_grab_frame(camera.region, cached)

    first = camera.grab(new_frame_only=False)
    second = camera.grab(new_frame_only=False)

    assert first is not None and second is not None
    assert not np.shares_memory(first, cached)
    assert not np.shares_memory(second, cached)
    assert not np.shares_memory(first, second)
    first.fill(255)
    np.testing.assert_array_equal(second, cached)
