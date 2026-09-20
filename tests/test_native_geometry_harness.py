from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np
import pytest


def load_benchmark(name):
    path = Path(__file__).parents[1] / "benchmarks" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


harness = load_benchmark("validate_native_geometry")
pattern_spec = load_benchmark("geometry_pattern").pattern_spec


def literal_canvas(spec):
    """Paint only literal bounds, without the verifier or DXcam conversions."""
    frame = np.zeros((spec["height"], spec["width"], 3), dtype=np.uint8)
    for rectangle in spec["rectangles"]:
        left, top, right, bottom = rectangle["rect"]
        frame[top:bottom, left:right] = rectangle["bgr"]
    return frame


@pytest.mark.parametrize("width,height", [(319, 197), (197, 319), (256, 256)])
def test_exact_literal_frame_passes_and_checks_epoch_samples(width, height):
    spec = pattern_spec(width, height, 0xA50F714D)
    result = harness.check_frame(literal_canvas(spec), spec)
    assert result["ok"]
    assert result["checked_samples"] == len(spec["samples"]) + 25
    assert result["failure_count"] == 0


@pytest.mark.parametrize("quarter_turns", [1, 2, 3])
def test_square_frame_rotation_is_detected_without_relying_on_shape(quarter_turns):
    spec = pattern_spec(256, 256, 19)
    frame = np.rot90(literal_canvas(spec), quarter_turns)
    result = harness.check_frame(frame, spec)
    assert not result["ok"]
    assert result["shape"] == [256, 256, 3]
    assert result["failure_count"] > 0


@pytest.mark.parametrize("axis", [0, 1])
def test_mirrored_frame_is_rejected(axis):
    spec = pattern_spec(319, 197, 19)
    result = harness.check_frame(np.flip(literal_canvas(spec), axis), spec)
    assert not result["ok"]
    assert result["failure_count"] > 0


@pytest.mark.parametrize("channels", [(2, 1, 0), (1, 0, 2), (0, 2, 1)])
def test_channel_permutations_are_rejected(channels):
    spec = pattern_spec(319, 197, 19)
    wrong_channels = literal_canvas(spec)[:, :, list(channels)]
    result = harness.check_frame(wrong_channels, spec)
    assert not result["ok"]
    assert result["failure_count"] > 0


def test_stale_epoch_fails_even_when_geometry_and_all_other_patches_match():
    first = pattern_spec(319, 197, 0x12345678)
    following = pattern_spec(319, 197, 0x12345679)
    result = harness.check_frame(literal_canvas(first), following)
    assert not result["ok"]
    assert result["failure_count"] >= 2
    failed_names = {failure["name"] for failure in result["failures"]}
    assert "epoch_bit_00_point" in failed_names
    assert "epoch_inverse_bit_00_point" in failed_names


def test_partially_updated_inverse_marker_fails():
    first = pattern_spec(319, 197, 1)
    following = pattern_spec(319, 197, 2)
    mixed = literal_canvas(following)
    for rectangle in first["rectangles"]:
        if rectangle["name"].startswith("epoch_inverse_"):
            left, top, right, bottom = rectangle["rect"]
            mixed[top:bottom, left:right] = rectangle["bgr"]
    result = harness.check_frame(mixed, following)
    assert not result["ok"]
    assert any(f["name"].startswith("epoch_inverse_") for f in result["failures"])


@pytest.mark.parametrize(
    "shape,dtype",
    [
        ((197, 318, 3), np.uint8),
        ((196, 319, 3), np.uint8),
        ((319, 197, 3), np.uint8),
        ((197, 319, 4), np.uint8),
        ((197, 319), np.uint8),
        ((197, 319, 3), np.float32),
        ((197, 319, 3), np.int8),
    ],
)
def test_wrong_shape_or_dtype_is_rejected_before_pixel_access(shape, dtype):
    spec = pattern_spec(319, 197)
    result = harness.check_frame(np.zeros(shape, dtype=dtype), spec)
    assert not result["ok"]
    assert result["shape"] == list(shape)
    assert result["expected_shape"] == [197, 319, 3]
    assert result["dtype"] == str(np.dtype(dtype))
    assert "checked_samples" not in result


@pytest.mark.parametrize(
    "region",
    [
        (20, 30, 200, 180),
        (199, 121, 319, 197),
        (0, 0, 137, 131),
        (318, 196, 319, 197),
    ],
)
def test_roi_uses_output_coordinates_for_expectations_and_local_array_indices(region):
    spec = pattern_spec(319, 197, 7)
    frame = literal_canvas(spec)
    left, top, right, bottom = region
    roi = frame[top:bottom, left:right]
    assert harness.check_frame(roi, spec, region)["ok"]


def test_roi_with_wrong_origin_is_detected_despite_correct_shape():
    spec = pattern_spec(319, 197, 7)
    frame = literal_canvas(spec)
    region = (20, 30, 200, 180)
    wrong_origin = frame[:150, :180]
    result = harness.check_frame(wrong_origin, spec, region)
    assert not result["ok"]
    assert result["shape"] == [150, 180, 3]
    assert result["failure_count"] > 0


def test_expected_pixel_respects_layer_order_and_half_open_rectangle_edges():
    spec = {
        "width": 8,
        "height": 7,
        "rectangles": [
            {"rect": [0, 0, 8, 7], "bgr": [10, 20, 30]},
            {"rect": [2, 1, 6, 5], "bgr": [40, 50, 60]},
            {"rect": [4, 3, 7, 6], "bgr": [70, 80, 90]},
        ],
        "samples": [],
    }
    assert harness.expected_pixel(spec, 0, 0) == [10, 20, 30]
    assert harness.expected_pixel(spec, 2, 1) == [40, 50, 60]
    assert harness.expected_pixel(spec, 3, 4) == [40, 50, 60]
    assert harness.expected_pixel(spec, 4, 3) == [70, 80, 90]
    assert harness.expected_pixel(spec, 6, 4) == [70, 80, 90]
    assert harness.expected_pixel(spec, 6, 2) == [10, 20, 30]
    assert harness.expected_pixel(spec, 3, 5) == [10, 20, 30]
    assert harness.expected_pixel(spec, 7, 6) == [10, 20, 30]
    for point in ((-1, 0), (0, -1), (8, 0), (0, 7)):
        with pytest.raises(ValueError, match="Pattern does not cover"):
            harness.expected_pixel(spec, *point)


@pytest.mark.parametrize("x,y", [(2, 3), (10, 3), (2, 11), (10, 11)])
def test_roi_grid_checks_every_corner_even_without_named_samples(x, y):
    spec = {
        "width": 13,
        "height": 15,
        "rectangles": [{"rect": [0, 0, 13, 15], "bgr": [17, 39, 83]}],
        "samples": [],
    }
    region = (2, 3, 11, 12)
    frame = np.full((9, 9, 3), [17, 39, 83], dtype=np.uint8)
    assert harness.check_frame(frame, spec, region)["ok"]
    frame[y - region[1], x - region[0]] = [83, 39, 17]
    result = harness.check_frame(frame, spec, region)
    assert not result["ok"]
    assert result["failure_count"] == 1
    assert result["failures"][0]["x"] == x
    assert result["failures"][0]["y"] == y


def test_one_pixel_bottom_right_roi_uses_last_pixel_without_out_of_bounds_access():
    spec = {
        "width": 13,
        "height": 15,
        "rectangles": [
            {"rect": [0, 0, 13, 15], "bgr": [1, 2, 3]},
            {"rect": [12, 14, 13, 15], "bgr": [5, 6, 7]},
        ],
        "samples": [],
    }
    frame = np.array([[[5, 6, 7]]], dtype=np.uint8)
    result = harness.check_frame(frame, spec, (12, 14, 13, 15))
    assert result["ok"]
    assert result["checked_samples"] == 25
    assert result["shape"] == [1, 1, 3]
