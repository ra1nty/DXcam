from __future__ import annotations

import logging
from typing import Any

import numpy as np
from numpy.typing import NDArray

from dxcam.types import ColorMode, Region
from .cv2_processor import (
    Cv2Processor,
    _NUMPY_IMPORT_ERROR,
    _NUMPY_KERNELS_AVAILABLE,
    _numpy_kernels,
)

logger = logging.getLogger(__name__)


class NumpyProcessor(Cv2Processor):
    """NumPy backend powered by Cython kernels with cv2 fallback.

    The Cython extension module is optional. If it is unavailable at runtime,
    this processor transparently falls back to :class:`Cv2Processor`.
    """

    _missing_extension_warned = False

    def __init__(self, color_mode: ColorMode) -> None:
        super().__init__(color_mode=color_mode)
        self._numpy_dst: NDArray[np.uint8] | None = None
        self._numpy_dst_shape: tuple[int, ...] | None = None
        self._numpy_contiguous_dst: NDArray[np.uint8] | None = None
        self._numpy_contiguous_dst_shape: tuple[int, ...] | None = None

    @classmethod
    def _warn_missing_extension_once(cls) -> None:
        if cls._missing_extension_warned:
            return
        cls._missing_extension_warned = True
        logger.warning(
            "NumPy processor backend requested but compiled extension "
            "'dxcam.processor._numpy_kernels' is unavailable; falling back to "
            "cv2 processor. Build with DXCAM_BUILD_CYTHON=1 and install "
            "the 'cython' extra to enable the accelerated backend.",
            exc_info=_NUMPY_IMPORT_ERROR is not None,
        )

    @staticmethod
    def _ensure_contiguous_uint8(image: NDArray[np.uint8]) -> NDArray[np.uint8]:
        if image.dtype != np.uint8 or not image.flags.c_contiguous:
            return np.ascontiguousarray(image, dtype=np.uint8)
        return image

    def _ensure_numpy_dst(self, height: int, width: int) -> NDArray[np.uint8]:
        if self._is_gray:
            dst_shape: tuple[int, ...] = (height, width, 1)
        else:
            dst_shape = (height, width, self._dst_channels)
        if self._numpy_dst is None or self._numpy_dst_shape != dst_shape:
            self._numpy_dst = np.empty(dst_shape, dtype=np.uint8)
            self._numpy_dst_shape = dst_shape
        return self._numpy_dst

    def _ensure_numpy_contiguous_dst(
        self,
        dst_shape: tuple[int, ...],
    ) -> NDArray[np.uint8]:
        if (
            self._numpy_contiguous_dst is None
            or self._numpy_contiguous_dst_shape != dst_shape
        ):
            self._numpy_contiguous_dst = np.empty(dst_shape, dtype=np.uint8)
            self._numpy_contiguous_dst_shape = dst_shape
        return self._numpy_contiguous_dst

    def process(
        self,
        rect: Any,
        width: int,
        height: int,
        region: Region,
        rotation_angle: int,
    ) -> NDArray[np.uint8]:
        if not _NUMPY_KERNELS_AVAILABLE:
            self._warn_missing_extension_once()
            return super().process(rect, width, height, region, rotation_angle)

        if self.color_mode is None:
            return super().process(rect, width, height, region, rotation_angle)

        image = self._prepare_image(rect, width, height, region, rotation_angle)
        assert _numpy_kernels is not None
        src = self._ensure_contiguous_uint8(image)
        dst = self._ensure_numpy_dst(height=src.shape[0], width=src.shape[1])
        _numpy_kernels.convert_bgra_into(src, dst, self.color_mode)
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
        if not _NUMPY_KERNELS_AVAILABLE:
            self._warn_missing_extension_once()
            super().process_into(rect, width, height, region, rotation_angle, dst)
            return

        if self.color_mode is None:
            super().process_into(rect, width, height, region, rotation_angle, dst)
            return

        image = self._prepare_image(rect, width, height, region, rotation_angle)
        assert _numpy_kernels is not None
        src = self._ensure_contiguous_uint8(image)
        if not dst.flags.c_contiguous:
            # Preserve behavior for non-contiguous destinations by converting
            # into a reusable contiguous temp and copying back.
            contiguous_dst = self._ensure_numpy_contiguous_dst(dst_shape=dst.shape)
            _numpy_kernels.convert_bgra_into(src, contiguous_dst, self.color_mode)
            np.copyto(dst, contiguous_dst, casting="no")
            return
        _numpy_kernels.convert_bgra_into(src, dst, self.color_mode)
