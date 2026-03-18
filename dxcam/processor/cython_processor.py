from __future__ import annotations

import ctypes
from importlib import import_module
from typing import Any

import numpy as np
from numpy.typing import NDArray

from dxcam.types import ColorMode, Region

try:
    _cython_kernels = import_module("dxcam.processor._cython_kernels")
    _CYTHON_KERNELS_AVAILABLE = True
    _CYTHON_IMPORT_ERROR: Exception | None = None
except Exception as exc:  # pragma: no cover - depends on local build env
    _cython_kernels = None
    _CYTHON_KERNELS_AVAILABLE = False
    _CYTHON_IMPORT_ERROR = exc


class CythonProcessor:
    """Cython-backed processor with no OpenCV dependency."""

    def __init__(self, color_mode: ColorMode) -> None:
        if not _CYTHON_KERNELS_AVAILABLE:
            raise RuntimeError(
                "Cython processor backend requested but compiled extension "
                "'dxcam.processor._cython_kernels' is unavailable. Build with "
                "DXCAM_BUILD_CYTHON=1 and install the 'cython' extra."
            ) from _CYTHON_IMPORT_ERROR

        self.color_mode = color_mode
        self._pbyte = ctypes.POINTER(ctypes.c_ubyte)
        self._cython_dst: NDArray[np.uint8] | None = None
        self._cython_dst_shape: tuple[int, ...] | None = None
        self._contiguous_dst: NDArray[np.uint8] | None = None
        self._contiguous_dst_shape: tuple[int, ...] | None = None

    def _dst_shape(self, height: int, width: int) -> tuple[int, ...]:
        if self.color_mode == "GRAY":
            return (height, width, 1)
        if self.color_mode in ("RGB", "BGR"):
            return (height, width, 3)
        return (height, width, 4)

    def _ensure_dst(self, height: int, width: int) -> NDArray[np.uint8]:
        dst_shape = self._dst_shape(height=height, width=width)
        if self._cython_dst is None or self._cython_dst_shape != dst_shape:
            self._cython_dst = np.empty(dst_shape, dtype=np.uint8)
            self._cython_dst_shape = dst_shape
        return self._cython_dst

    def _ensure_contiguous_dst(
        self,
        dst_shape: tuple[int, ...],
    ) -> NDArray[np.uint8]:
        if self._contiguous_dst is None or self._contiguous_dst_shape != dst_shape:
            self._contiguous_dst = np.empty(dst_shape, dtype=np.uint8)
            self._contiguous_dst_shape = dst_shape
        return self._contiguous_dst

    def _map_rect_bytes(
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
        return np.ctypeslib.as_array(buffer, shape=(size,)), pitch

    def _process_flat_into(
        self,
        rect: Any,
        width: int,
        height: int,
        region: Region,
        rotation_angle: int,
        dst: NDArray[np.uint8],
    ) -> None:
        src, pitch = self._map_rect_bytes(
            rect=rect,
            width=width,
            height=height,
            rotation_angle=rotation_angle,
        )
        assert _cython_kernels is not None
        _cython_kernels.process_bgra_into(
            src,
            pitch,
            width,
            height,
            region,
            rotation_angle,
            self.color_mode,
            dst.reshape(-1),
        )

    def process(
        self,
        rect: Any,
        width: int,
        height: int,
        region: Region,
        rotation_angle: int,
    ) -> NDArray[np.uint8]:
        out_h = region[3] - region[1]
        out_w = region[2] - region[0]
        dst = self._ensure_dst(height=out_h, width=out_w)
        self._process_flat_into(
            rect=rect,
            width=width,
            height=height,
            region=region,
            rotation_angle=rotation_angle,
            dst=dst,
        )
        return dst

    def process_into(
        self,
        rect: Any,
        width: int,
        height: int,
        region: Region,
        rotation_angle: int,
        dst: NDArray[np.uint8],
    ) -> None:
        if dst.flags.c_contiguous:
            self._process_flat_into(
                rect=rect,
                width=width,
                height=height,
                region=region,
                rotation_angle=rotation_angle,
                dst=dst,
            )
            return

        contiguous_dst = self._ensure_contiguous_dst(dst_shape=dst.shape)
        self._process_flat_into(
            rect=rect,
            width=width,
            height=height,
            region=region,
            rotation_angle=rotation_angle,
            dst=contiguous_dst,
        )
        np.copyto(dst, contiguous_dst, casting="no")
