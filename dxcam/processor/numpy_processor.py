from __future__ import annotations

import ctypes
from importlib import import_module
from typing import Any, Callable

import numpy as np
from numpy.typing import NDArray

from dxcam.types import ColorMode, Region
from .base import Processor


class NumpyProcessor(Processor):
    """NumPy-based frame processor with optional OpenCV color conversion."""

    def __init__(self, color_mode: ColorMode) -> None:
        self.cvtcolor: Callable[[NDArray[np.uint8]], NDArray[np.uint8]] | None = None
        self.color_mode = color_mode

    def process_cvtcolor(self, image: NDArray[np.uint8]) -> NDArray[np.uint8]:
        cv2 = import_module("cv2")

        # only one time process
        if self.cvtcolor is None:
            color_mapping: dict[str, int | None] = {
                "RGB": cv2.COLOR_BGRA2RGB,
                "RGBA": cv2.COLOR_BGRA2RGBA,
                "BGR": cv2.COLOR_BGRA2BGR,
                "GRAY": cv2.COLOR_BGRA2GRAY,
                "BGRA": None,
            }
            cv2_code = color_mapping[self.color_mode]
            if cv2_code is not None:
                if cv2_code != cv2.COLOR_BGRA2GRAY:
                    self.cvtcolor = lambda image: cv2.cvtColor(image, cv2_code)
                else:
                    self.cvtcolor = lambda image: cv2.cvtColor(image, cv2_code)[
                        ..., np.newaxis
                    ]
            else:
                return image
        assert self.cvtcolor is not None
        return self.cvtcolor(image)

    def process(
        self,
        rect: Any,
        width: int,
        height: int,
        region: Region,
        rotation_angle: int,
    ) -> NDArray[np.uint8]:
        pitch = int(rect.Pitch)

        if rotation_angle in (0, 180):
            size = pitch * height
        else:
            size = pitch * width

        buffer = ctypes.string_at(rect.pBits, size)
        pitch = pitch // 4
        if rotation_angle in (0, 180):
            image = np.ndarray((height, pitch, 4), dtype=np.uint8, buffer=buffer)
        elif rotation_angle in (90, 270):
            image = np.ndarray((width, pitch, 4), dtype=np.uint8, buffer=buffer)
        else:
            raise ValueError(f"Unsupported rotation angle: {rotation_angle}")

        if self.color_mode is not None:
            image = self.process_cvtcolor(image)

        if rotation_angle == 90:
            image = np.rot90(image, axes=(1, 0))
        elif rotation_angle == 180:
            image = np.rot90(image, k=2, axes=(0, 1))
        elif rotation_angle == 270:
            image = np.rot90(image, axes=(0, 1))

        if rotation_angle in (0, 180) and pitch != width:
            image = image[:, :width, :]
        elif rotation_angle in (90, 270) and pitch != height:
            image = image[:height, :, :]

        if region[2] - region[0] != width or region[3] - region[1] != height:
            image = image[region[1] : region[3], region[0] : region[2], :]

        return image
