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


def fake_geometry(backend, rotation=0):
    """A writable native descriptor with a separate, deliberately stale cache."""
    import ctypes
    from types import SimpleNamespace

    from dxcam._libs.dxgi import DXGI_OUTPUT_DESC

    native = DXGI_OUTPUT_DESC()
    native.DesktopCoordinates.left = -320
    native.DesktopCoordinates.top = 75
    native.DesktopCoordinates.right = 0
    native.DesktopCoordinates.bottom = 315
    native.Rotation = {0: 1, 90: 2, 180: 3, 270: 4}[rotation]
    native.AttachedToDesktop = True
    cached = DXGI_OUTPUT_DESC()
    cached.DesktopCoordinates.right = 99
    cached.DesktopCoordinates.bottom = 77
    cached.Rotation = 1

    class NativeOutput:
        calls = 0

        def GetDesc(self, pointer):
            self.calls += 1
            ctypes.memmove(pointer, ctypes.byref(native), ctypes.sizeof(native))

    output = SimpleNamespace(output=NativeOutput(), desc=cached)
    camera = SimpleNamespace(
        backend=backend,
        width=320,
        height=240,
        rotation_angle=rotation,
        _capture_rotation_angle=rotation if backend == "dxgi" else 0,
        _output=output,
        _duplicator=SimpleNamespace(_frame_pool_dimensions=(320, 240)),
    )
    size = [240, 320] if backend == "dxgi" and rotation in (90, 270) else [320, 240]
    observations = {
        "copies": [
            {"source_texture": [99, 77], "stage_size": [99, 77], "texture_format": 0},
            {
                "source_texture": size.copy(),
                "stage_size": size.copy(),
                "texture_format": 87,
            },
        ]
    }
    return camera, {"width": 320, "height": 240}, observations, native


@pytest.mark.parametrize("backend", ["dxgi", "winrt"])
@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_geometry_uses_fresh_native_descriptor_and_backend_specific_surface_size(
    backend, rotation
):
    camera, spec, observations, _ = fake_geometry(backend, rotation)
    cached = camera._output.desc
    result = harness.verify_geometry(camera, spec, observations)
    assert result["ok"]
    assert all(result["checks"].values())
    assert camera._output.output.calls == 1
    assert result["native_size"] == [320, 240]
    assert result["native_rotation"] == rotation
    assert result["last_copy"] is observations["copies"][-1]
    expected_source = (
        [240, 320] if backend == "dxgi" and rotation in (90, 270) else [320, 240]
    )
    assert result["expected_source_texture"] == expected_source
    assert camera._output.desc is cached
    assert cached.DesktopCoordinates.right == 99
    assert cached.DesktopCoordinates.bottom == 77
    assert cached.Rotation == 1
    assert ("pool_dimensions" in result["checks"]) == (backend == "winrt")


def test_wgc_stale_180_degree_metadata_is_rejected_despite_unchanged_dimensions():
    camera, spec, observations, _ = fake_geometry("winrt", 180)
    camera.rotation_angle = 0
    result = harness.verify_geometry(camera, spec, observations)
    assert not result["ok"]
    assert result["checks"]["public_rotation"] is False
    assert all(
        value for name, value in result["checks"].items() if name != "public_rotation"
    )
    assert result["native_rotation"] == 180
    assert result["public_rotation"] == 0


@pytest.mark.parametrize(
    "field,value,failed_check",
    [
        ("width", 319, "public_size"),
        ("height", 239, "public_size"),
        ("rotation_angle", 0, "public_rotation"),
        ("_capture_rotation_angle", 0, "effective_rotation"),
    ],
)
def test_geometry_rejects_stale_camera_fields(field, value, failed_check):
    camera, spec, observations, _ = fake_geometry("dxgi", 90)
    setattr(camera, field, value)
    result = harness.verify_geometry(camera, spec, observations)
    assert not result["ok"]
    assert result["checks"][failed_check] is False


@pytest.mark.parametrize(
    "field,value,failed_check",
    [
        ("source_texture", [320, 240], "source_texture"),
        ("stage_size", [320, 240], "staging_size"),
        ("texture_format", 28, "source_format"),
    ],
)
def test_geometry_rejects_incorrect_copied_surface_metadata(field, value, failed_check):
    camera, spec, observations, _ = fake_geometry("dxgi", 270)
    observations["copies"][-1][field] = value
    result = harness.verify_geometry(camera, spec, observations)
    assert not result["ok"]
    assert result["checks"][failed_check] is False


def test_geometry_requires_at_least_one_observed_copy():
    camera, spec, observations, _ = fake_geometry("dxgi")
    observations["copies"] = []
    result = harness.verify_geometry(camera, spec, observations)
    assert not result["ok"]
    assert result["last_copy"] == {}
    assert result["checks"]["source_texture"] is False
    assert result["checks"]["staging_size"] is False
    assert result["checks"]["source_format"] is False


def test_geometry_rejects_native_mode_that_does_not_match_pattern_dimensions():
    camera, spec, observations, native = fake_geometry("winrt")
    native.DesktopCoordinates.right += 80
    result = harness.verify_geometry(camera, spec, observations)
    assert not result["ok"]
    assert result["native_size"] == [400, 240]
    assert result["checks"]["native_size"] is False


def test_wgc_rejects_stale_pool_size_and_unnecessary_rotation():
    camera, spec, observations, _ = fake_geometry("winrt", 90)
    camera._capture_rotation_angle = 90
    camera._duplicator._frame_pool_dimensions = (240, 320)
    result = harness.verify_geometry(camera, spec, observations)
    assert not result["ok"]
    assert result["checks"]["effective_rotation"] is False
    assert result["checks"]["pool_dimensions"] is False
    assert result["checks"]["source_texture"] is True
    assert result["expected_source_texture"] == [320, 240]


def test_native_descriptor_read_failure_is_not_reported_as_success():
    camera, spec, observations, _ = fake_geometry("dxgi")

    def fail_get_desc(pointer):
        raise OSError("Native output is unavailable")

    camera._output.output.GetDesc = fail_get_desc
    with pytest.raises(OSError, match="Native output is unavailable"):
        harness.verify_geometry(camera, spec, observations)


@pytest.mark.parametrize(
    "name", ["validate_native_geometry", "geometry_pattern", "geometry_display"]
)
def test_loading_geometry_helper_does_not_redirect_installed_package_imports(name):
    import sys

    original_path = sys.path
    original_entries = tuple(sys.path)
    loaded = load_benchmark(name)
    assert loaded is not None
    assert sys.path is original_path
    assert tuple(sys.path) == original_entries


@pytest.mark.parametrize(
    "width,height", [(128, 128), (128, 319), (319, 128), (319, 197)]
)
def test_all_roi_cases_fit_minimum_pattern_size_and_have_matching_pixels(width, height):
    spec = pattern_spec(width, height, 5)
    frame = literal_canvas(spec)
    regions = harness.roi_cases(width, height)
    assert regions[-1] == (width - 1, height - 1, width, height)
    for left, top, right, bottom in regions:
        assert 0 <= left < right <= width
        assert 0 <= top < bottom <= height
        assert harness.check_frame(
            frame[top:bottom, left:right], spec, (left, top, right, bottom)
        )["ok"]
