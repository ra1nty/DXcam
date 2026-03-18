from __future__ import annotations

import numpy as np

from dxcam.types import Frame, Region


def validate_region(region: Region, width: int, height: int) -> None:
    left, top, right, bottom = region
    if not (width >= right > left >= 0 and height >= bottom > top >= 0):
        raise ValueError(f"Invalid Region: Region should be in {width}x{height}")


def resolve_capture_copy_spec(
    region: Region,
    rotation_angle: int,
    surface_size: tuple[int, int],
) -> tuple[Region, int, int, int, int]:
    surface_width, surface_height = surface_size
    if rotation_angle == 0:
        memory_region = region
    elif rotation_angle == 90:
        memory_region = (
            region[1],
            surface_height - region[2],
            region[3],
            surface_height - region[0],
        )
    elif rotation_angle == 180:
        memory_region = (
            surface_width - region[2],
            surface_height - region[3],
            surface_width - region[0],
            surface_height - region[1],
        )
    elif rotation_angle == 270:
        memory_region = (
            surface_width - region[3],
            region[0],
            surface_width - region[1],
            region[2],
        )
    else:
        raise ValueError(f"Unsupported rotation angle: {rotation_angle}")

    frame_width = region[2] - region[0]
    frame_height = region[3] - region[1]
    memory_width = memory_region[2] - memory_region[0]
    memory_height = memory_region[3] - memory_region[1]
    return memory_region, frame_width, frame_height, memory_width, memory_height


def allocate_output_frame(
    frame_width: int,
    frame_height: int,
    channel_size: int,
) -> Frame:
    return np.empty((frame_height, frame_width, channel_size), dtype=np.uint8)


def validate_destination_frame(
    dst: Frame,
    *,
    frame_width: int,
    frame_height: int,
    channel_size: int,
) -> None:
    expected_shape = (frame_height, frame_width, channel_size)
    if dst.shape != expected_shape:
        raise ValueError(
            f"Destination frame shape mismatch: expected {expected_shape}, got {dst.shape}."
        )
    if dst.dtype != np.uint8:
        raise ValueError(
            f"Destination frame dtype mismatch: expected uint8, got {dst.dtype}."
        )
