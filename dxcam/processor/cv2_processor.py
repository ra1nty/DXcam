from __future__ import annotations

import ctypes
from importlib import import_module
from typing import Any, Callable

import numpy as np
from numpy.typing import NDArray

from dxcam.types import ColorMode, Region

try:
    _numpy_kernels = import_module("dxcam.processor._numpy_kernels")
    _NUMPY_KERNELS_AVAILABLE = True
    _NUMPY_IMPORT_ERROR: Exception | None = None
except Exception as exc:  # pragma: no cover - depends on local build env
    _numpy_kernels = None
    _NUMPY_KERNELS_AVAILABLE = False
    _NUMPY_IMPORT_ERROR = exc


class Cv2Processor:
    """cv2-first frame processor with shared BGRA preparation helpers.

    ``process()`` may return an internal reusable buffer for some modes.
    Use ``process_into()`` when caller-owned output memory is required.
    """

    def __init__(self, color_mode: ColorMode) -> None:
        self._cv2: Any | None = None
        self._cv2_code: int | None = None
        self._cv2_rotate_mod: Any | None = None
        self._cv2_dst: NDArray[np.uint8] | None = None
        self._cv2_dst_shape: tuple[int, ...] | None = None
        self._prepared_bgra: NDArray[np.uint8] | None = None
        self._prepared_bgra_shape: tuple[int, int, int] | None = None
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

    @staticmethod
    def _region_is_full_frame(region: Region, width: int, height: int) -> bool:
        return region == (0, 0, width, height)

    @staticmethod
    def _trim_pitch_for_rotation(
        image: NDArray[np.uint8],
        width: int,
        height: int,
        rotation_angle: int,
    ) -> NDArray[np.uint8]:
        active_cols = width if rotation_angle in (0, 180) else height
        if image.shape[1] < active_cols:
            raise ValueError(
                "Mapped image pitch is smaller than required active width: "
                f"pitch_pixels={image.shape[1]}, required={active_cols}."
            )
        if image.shape[1] != active_cols:
            image = image[:, :active_cols, :]
        return image

    @staticmethod
    def _crop_view(image: NDArray[np.uint8], region: Region) -> NDArray[np.uint8]:
        return image[region[1] : region[3], region[0] : region[2], :]

    def _map_rect_as_image(
        self,
        rect: Any,
        width: int,
        height: int,
        rotation_angle: int,
    ) -> NDArray[np.uint8]:
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
        return byte_view.reshape((rows, pitch_pixels, 4))

    def _ensure_cvtcolor_dst(self, height: int, width: int) -> NDArray[np.uint8]:
        if self._is_gray:
            dst_shape: tuple[int, ...] = (height, width)
        else:
            dst_shape = (height, width, self._dst_channels)
        if self._cv2_dst is None or self._cv2_dst_shape != dst_shape:
            self._cv2_dst = np.empty(dst_shape, dtype=np.uint8)
            self._cv2_dst_shape = dst_shape
        return self._cv2_dst

    def _ensure_cvtcolor_initialized(self) -> None:
        if self._cv2 is not None and self._cv2_code is not None:
            return
        self._cv2 = import_module("cv2")
        self._cv2_rotate_mod = self._cv2
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

    def _init_cvtcolor_impl(self, image: NDArray[np.uint8]) -> NDArray[np.uint8]:
        self._ensure_cvtcolor_initialized()
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

    def _ensure_prepared_bgra_dst(self, height: int, width: int) -> NDArray[np.uint8]:
        dst_shape = (height, width, 4)
        if self._prepared_bgra is None or self._prepared_bgra_shape != dst_shape:
            self._prepared_bgra = np.empty(dst_shape, dtype=np.uint8)
            self._prepared_bgra_shape = dst_shape
        return self._prepared_bgra

    def _get_cv2_rotate_module(self) -> Any | None:
        if self._cv2_rotate_mod is not None:
            return self._cv2_rotate_mod
        try:
            self._cv2_rotate_mod = import_module("cv2")
        except Exception:
            self._cv2_rotate_mod = None
        return self._cv2_rotate_mod

    def _prepare_image_python(
        self,
        image: NDArray[np.uint8],
        width: int,
        height: int,
        region: Region,
        rotation_angle: int,
    ) -> NDArray[np.uint8]:
        # Trim DXGI row-pitch padding before rotation so 180/270 paths do not
        # rotate padded columns into visible pixels.
        image = self._trim_pitch_for_rotation(
            image=image,
            width=width,
            height=height,
            rotation_angle=rotation_angle,
        )

        if rotation_angle in (90, 180, 270):
            cv2_mod = self._get_cv2_rotate_module()
            if cv2_mod is not None:
                if rotation_angle == 90:
                    image = cv2_mod.rotate(image, cv2_mod.ROTATE_90_CLOCKWISE)
                elif rotation_angle == 180:
                    image = cv2_mod.rotate(image, cv2_mod.ROTATE_180)
                else:
                    image = cv2_mod.rotate(
                        image,
                        cv2_mod.ROTATE_90_COUNTERCLOCKWISE,
                    )
            else:
                if rotation_angle == 90:
                    image = np.rot90(image, axes=(1, 0))
                elif rotation_angle == 180:
                    image = np.rot90(image, k=2, axes=(0, 1))
                else:
                    image = np.rot90(image, axes=(0, 1))

        if not self._region_is_full_frame(region=region, width=width, height=height):
            image = self._crop_view(image=image, region=region)

        return image

    def _prepare_bgra_into(
        self,
        rect: Any,
        width: int,
        height: int,
        region: Region,
        rotation_angle: int,
        dst: NDArray[np.uint8],
    ) -> None:
        image = self._map_rect_as_image(rect, width, height, rotation_angle)
        full_region = self._region_is_full_frame(
            region=region, width=width, height=height
        )

        if rotation_angle == 0:
            image = self._trim_pitch_for_rotation(
                image=image,
                width=width,
                height=height,
                rotation_angle=rotation_angle,
            )
            view = image if full_region else self._crop_view(image=image, region=region)
            np.copyto(dst, view, casting="no")
            return

        if (
            _NUMPY_KERNELS_AVAILABLE
            and dst.flags.c_contiguous
            and dst.dtype == np.uint8
        ):
            assert _numpy_kernels is not None
            _numpy_kernels.prepare_bgra_into(
                image,
                dst,
                width,
                height,
                region,
                rotation_angle,
            )
            return

        image = self._prepare_image_python(
            image=image,
            width=width,
            height=height,
            region=region,
            rotation_angle=rotation_angle,
        )
        np.copyto(dst, image, casting="no")

    def _prepare_image(
        self,
        rect: Any,
        width: int,
        height: int,
        region: Region,
        rotation_angle: int,
    ) -> NDArray[np.uint8]:
        image = self._map_rect_as_image(rect, width, height, rotation_angle)

        # Fast path: no rotation. Keep mapped view (+ pitch trim) and apply
        # optional cheap crop view to avoid unnecessary prep copies.
        if rotation_angle == 0:
            image = self._trim_pitch_for_rotation(
                image=image,
                width=width,
                height=height,
                rotation_angle=rotation_angle,
            )
            if self._region_is_full_frame(region=region, width=width, height=height):
                return image
            return self._crop_view(image=image, region=region)

        if _NUMPY_KERNELS_AVAILABLE:
            assert _numpy_kernels is not None
            out_h = region[3] - region[1]
            out_w = region[2] - region[0]
            dst = self._ensure_prepared_bgra_dst(height=out_h, width=out_w)
            _numpy_kernels.prepare_bgra_into(
                image,
                dst,
                width,
                height,
                region,
                rotation_angle,
            )
            return dst

        return self._prepare_image_python(
            image=image,
            width=width,
            height=height,
            region=region,
            rotation_angle=rotation_angle,
        )

    def process(
        self,
        rect: Any,
        width: int,
        height: int,
        region: Region,
        rotation_angle: int,
    ) -> NDArray[np.uint8]:
        if self.color_mode is None:
            out_h = region[3] - region[1]
            out_w = region[2] - region[0]
            # Reuse a stable BGRA destination buffer on process() calls.
            # This mirrors RGB/BGR/RGBA process() behavior, which already
            # returns an internal reusable buffer.
            dst = self._ensure_prepared_bgra_dst(height=out_h, width=out_w)
            self._prepare_bgra_into(
                rect=rect,
                width=width,
                height=height,
                region=region,
                rotation_angle=rotation_angle,
                dst=dst,
            )
            return dst

        image = self._prepare_image(rect, width, height, region, rotation_angle)
        return self.process_cvtcolor(image)

    def process_into(
        self,
        rect: Any,
        width: int,
        height: int,
        region: Region,
        rotation_angle: int,
        dst: NDArray[np.uint8],
    ) -> None:
        if self.color_mode is None:
            self._prepare_bgra_into(
                rect=rect,
                width=width,
                height=height,
                region=region,
                rotation_angle=rotation_angle,
                dst=dst,
            )
            return

        image = self._prepare_image(rect, width, height, region, rotation_angle)
        self._ensure_cvtcolor_initialized()
        assert self._cv2 is not None
        assert self._cv2_code is not None
        if self._is_gray:
            self._cv2.cvtColor(image, self._cv2_code, dst=dst[..., 0])
        else:
            self._cv2.cvtColor(image, self._cv2_code, dst=dst)
