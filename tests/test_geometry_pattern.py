from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import numpy as np
import pytest


_spec = importlib.util.spec_from_file_location(
    "geometry_pattern", Path(__file__).parents[1] / "benchmarks" / "geometry_pattern.py"
)
assert _spec is not None and _spec.loader is not None
pattern_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pattern_module)
pattern_spec = pattern_module.pattern_spec


@pytest.mark.parametrize(
    "width,height", [(128, 128), (319, 197), (1920, 1080), (1080, 1920)]
)
def test_expected_samples_match_rectangles_in_an_independent_canvas(width, height):
    spec = pattern_spec(width, height, 0xA53C129B)
    canvas = np.zeros((height, width, 3), dtype=np.uint8)
    for rectangle in spec["rectangles"]:
        left, top, right, bottom = rectangle["rect"]
        assert 0 <= left < right <= width
        assert 0 <= top < bottom <= height
        assert all(
            isinstance(value, int) and 0 <= value <= 255 for value in rectangle["bgr"]
        )
        canvas[top:bottom, left:right] = rectangle["bgr"]
    for sample in spec["samples"]:
        assert 0 <= sample["x"] < width and 0 <= sample["y"] < height
        assert canvas[sample["y"], sample["x"]].tolist() == sample["bgr"]
    assert len({sample["name"] for sample in spec["samples"]}) == len(spec["samples"])
    assert json.loads(json.dumps(spec)) == spec


def test_corner_labels_colors_and_edge_samples_are_distinct_and_exact():
    spec = pattern_spec(319, 197)
    samples = {sample["name"]: sample for sample in spec["samples"]}
    for name, color in (
        ("top_left", [0, 0, 255]),
        ("top_right", [0, 255, 0]),
        ("bottom_left", [255, 0, 0]),
        ("bottom_right", [0, 255, 255]),
    ):
        assert samples[f"{name}_center"]["bgr"] == color
        assert samples[f"{name}_near_left"]["bgr"] == color
        assert samples[f"{name}_near_right"]["bgr"] == color
        assert any(
            rect["name"].startswith(f"label_{name}_") for rect in spec["rectangles"]
        )
    for edge in ("top", "bottom", "left", "right"):
        assert samples[f"{edge}_edge"]["bgr"] == [32, 32, 32]


@pytest.mark.parametrize("epoch", [1, 2, 255, 256, 0x7FFFFFFF, 0x80000000, 0xFFFFFFFF])
def test_epoch_and_inverse_marker_encode_every_bit(epoch):
    spec = pattern_spec(128, 128, epoch)
    samples = {sample["name"]: sample for sample in spec["samples"]}
    actual = 0
    inverse = 0
    for bit in range(32):
        value = samples[f"epoch_bit_{bit:02d}_point"]["bgr"]
        inverted = samples[f"epoch_inverse_bit_{bit:02d}_point"]["bgr"]
        assert value in ([0, 0, 0], [255, 255, 255])
        assert inverted == [255 - channel for channel in value]
        actual |= int(value[0] == 255) << bit
        inverse |= int(inverted[0] == 255) << bit
    assert actual == epoch
    assert inverse == epoch ^ 0xFFFFFFFF


def test_epoch_change_alters_marker_and_preserves_geometry():
    first, second = pattern_spec(319, 197, 7), pattern_spec(319, 197, 8)
    assert first["samples"] != second["samples"]
    assert [rect["rect"] for rect in first["rectangles"]] == [
        rect["rect"] for rect in second["rectangles"]
    ]

    def regular(spec):
        return [
            rect for rect in spec["rectangles"] if not rect["name"].startswith("epoch_")
        ]

    assert regular(first) == regular(second)


def test_pattern_distinguishes_axes_and_contains_an_offcenter_shape():
    spec = pattern_spec(320, 240)
    rectangles = {rectangle["name"]: rectangle for rectangle in spec["rectangles"]}
    horizontal, vertical = rectangles["horizontal_band"], rectangles["vertical_band"]
    left, top, right, bottom = horizontal["rect"]
    assert right - left > bottom - top
    left, top, right, bottom = vertical["rect"]
    assert bottom - top > right - left
    assert horizontal["bgr"] != vertical["bgr"]
    stem, foot = (
        rectangles["offcenter_stem"]["rect"],
        rectangles["offcenter_foot"]["rect"],
    )
    assert stem[0] == foot[0] and stem[2] < foot[2]
    assert stem[1] < foot[1] and stem[3] == foot[3]
    assert foot[2] < spec["width"] // 2


@pytest.mark.parametrize(
    "width,height,epoch",
    [
        (127, 128, 1),
        (128, 127, 1),
        (32769, 128, 1),
        (128, 32769, 1),
        (128.0, 128, 1),
        (128, True, 1),
        (128, 128, 0),
        (128, 128, -1),
        (128, 128, 2**32),
        (128, 128, 1.0),
        (128, 128, True),
    ],
)
def test_invalid_inputs_fail_before_any_windows_api_calls(width, height, epoch):
    with pytest.raises(ValueError):
        pattern_spec(width, height, epoch)
