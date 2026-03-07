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
        self._cv2: Any | None = None
        self._cv2_code: int | None = None
        self._cv2_dst: NDArray[np.uint8] | None = None
        self._cv2_dst_shape: tuple[int, ...] | None = None
        self.color_mode: ColorMode | None = None if color_mode == "BGRA" else color_mode
        self._pbyte = ctypes.POINTER(ctypes.c_ubyte)
        self._cvtcolor_impl: Callable[[NDArray[np.uint8]], NDArray[np.uint8]] = (
            self._init_cvtcolor_impl
        )
        self._is_gray = self.color_mode == "GRAY"
        if self.color_mode in ("RGB", "BGR"):
            self._dst_channels = 3
        elif self.color_mode == "RGBA":
            self._dst_channels = 4
        else:
            self._dst_channels = 1

    def _map_rect_as_image(
        self,
        rect: Any,
        width: int,
        height: int,
        rotation_angle: int,
    ) -> tuple[NDArray[np.uint8], int]:
        pitch = int(rect.Pitch)
        if pitch <= 0:
            raise ValueError(f"Invalid mapped pitch: {pitch}")
        if pitch % 4 != 0:
            raise ValueError(f"Unsupported BGRA pitch alignment: {pitch}")

        rows = height if rotation_angle in (0, 180) else width
        size = pitch * rows
        buffer = ctypes.cast(rect.pBits, self._pbyte)
        byte_view: NDArray[np.uint8] = np.ctypeslib.as_array(buffer, shape=(size,))
        pitch_pixels = pitch // 4
        image = byte_view.reshape((rows, pitch_pixels, 4))
        return image, pitch_pixels

    def _ensure_cvtcolor_dst(self, height: int, width: int) -> NDArray[np.uint8]:
        if self._is_gray:
            dst_shape: tuple[int, ...] = (height, width)
        else:
            dst_shape = (height, width, self._dst_channels)
        if self._cv2_dst is None or self._cv2_dst_shape != dst_shape:
            self._cv2_dst = np.empty(dst_shape, dtype=np.uint8)
            self._cv2_dst_shape = dst_shape
        return self._cv2_dst

    def _init_cvtcolor_impl(self, image: NDArray[np.uint8]) -> NDArray[np.uint8]:
        self._cv2 = import_module("cv2")
        color_mapping: dict[str, int] = {
            "RGB": self._cv2.COLOR_BGRA2RGB,
            "BGR": self._cv2.COLOR_BGRA2BGR,
            "RGBA": self._cv2.COLOR_BGRA2RGBA,
            "GRAY": self._cv2.COLOR_BGRA2GRAY,
        }
        assert self.color_mode is not None
        self._cv2_code = color_mapping[self.color_mode]
        if self._is_gray:
            self._cvtcolor_impl = self._process_cvtcolor_gray
        else:
            self._cvtcolor_impl = self._process_cvtcolor_color
        return self._cvtcolor_impl(image)

    def _process_cvtcolor_color(self, image: NDArray[np.uint8]) -> NDArray[np.uint8]:
        height, width = image.shape[:2]
        dst = self._ensure_cvtcolor_dst(height=height, width=width)
        assert self._cv2 is not None
        assert self._cv2_code is not None
        self._cv2.cvtColor(image, self._cv2_code, dst=dst)
        return dst

    def _process_cvtcolor_gray(self, image: NDArray[np.uint8]) -> NDArray[np.uint8]:
        height, width = image.shape[:2]
        dst = self._ensure_cvtcolor_dst(height=height, width=width)
        assert self._cv2 is not None
        assert self._cv2_code is not None
        self._cv2.cvtColor(image, self._cv2_code, dst=dst)
        return dst[..., np.newaxis]

    def process_cvtcolor(self, image: NDArray[np.uint8]) -> NDArray[np.uint8]:
        return self._cvtcolor_impl(image)

    def process(
        self,
        rect: Any,
        width: int,
        height: int,
        region: Region,
        rotation_angle: int,
    ) -> NDArray[np.uint8]:
        image, pitch = self._map_rect_as_image(rect, width, height, rotation_angle)

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

        # BGRA mode may still reference mapped desktop memory. Copy so callers
        # can safely use the returned frame after IDXGISurface.Unmap().
        if self.color_mode is None:
            return np.array(image, copy=True)

        return self.process_cvtcolor(image)
