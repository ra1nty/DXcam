# cython: language_level=3
# cython: boundscheck=False
# cython: wraparound=False
# cython: initializedcheck=False
# cython: nonecheck=False
# cython: cdivision=True

from libc.stdint cimport uint8_t, uint32_t
from libc.string cimport memcpy
from cython.parallel cimport prange
import os

cdef Py_ssize_t _DEFAULT_PARALLEL_PIXELS_THRESHOLD = 128 * 128
cdef Py_ssize_t _PARALLEL_PIXELS_THRESHOLD = _DEFAULT_PARALLEL_PIXELS_THRESHOLD


def get_parallel_pixels_threshold() -> int:
    return int(_PARALLEL_PIXELS_THRESHOLD)


def set_parallel_pixels_threshold(value: int) -> None:
    global _PARALLEL_PIXELS_THRESHOLD
    if value < 0:
        raise ValueError("parallel threshold must be >= 0")
    _PARALLEL_PIXELS_THRESHOLD = <Py_ssize_t>value


def reset_parallel_pixels_threshold() -> None:
    global _PARALLEL_PIXELS_THRESHOLD
    _PARALLEL_PIXELS_THRESHOLD = _DEFAULT_PARALLEL_PIXELS_THRESHOLD


def _initialize_tuning_from_env() -> None:
    raw = os.environ.get("DXCAM_CYTHON_PARALLEL_THRESHOLD")
    if raw is None:
        return
    try:
        value = int(raw)
    except ValueError:
        value = -1
    if value >= 0:
        set_parallel_pixels_threshold(value)


_initialize_tuning_from_env()


cdef enum _ModeCode:
    MODE_BGRA = 0
    MODE_RGB = 1
    MODE_BGR = 2
    MODE_RGBA = 3
    MODE_GRAY = 4


cdef inline int _mode_to_code(str mode) except -1:
    if mode == "BGRA":
        return MODE_BGRA
    if mode == "RGB":
        return MODE_RGB
    if mode == "BGR":
        return MODE_BGR
    if mode == "RGBA":
        return MODE_RGBA
    if mode == "GRAY":
        return MODE_GRAY
    raise ValueError(
        f"Unsupported output mode '{mode}'. Supported modes: BGRA, RGB, BGR, RGBA, GRAY."
    )


cdef inline int _validate_rotation_angle(int rotation_angle) except -1:
    if rotation_angle == 0:
        return 0
    if rotation_angle == 90:
        return 90
    if rotation_angle == 180:
        return 180
    if rotation_angle == 270:
        return 270
    raise ValueError(
        "Unsupported rotation angle. Supported values are 0, 90, 180, 270."
    )


cdef inline void _source_step_params(
    Py_ssize_t y,
    Py_ssize_t width,
    Py_ssize_t height,
    Py_ssize_t left,
    Py_ssize_t top,
    Py_ssize_t pitch,
    int rotation_angle,
    Py_ssize_t* si_start,
    Py_ssize_t* si_step,
) noexcept nogil:
    if rotation_angle == 0:
        si_start[0] = (top + y) * pitch + (left * 4)
        si_step[0] = 4
        return
    if rotation_angle == 180:
        si_start[0] = (height - 1 - (top + y)) * pitch + (width - 1 - left) * 4
        si_step[0] = -4
        return
    if rotation_angle == 90:
        si_start[0] = (width - 1 - left) * pitch + (top + y) * 4
        si_step[0] = -pitch
        return
    si_start[0] = left * pitch + (height - 1 - (top + y)) * 4
    si_step[0] = pitch


cdef inline void _process_bgra_serial_ptr(
    const uint8_t* src_ptr,
    uint8_t* dst_ptr,
    Py_ssize_t pitch,
    Py_ssize_t width,
    Py_ssize_t height,
    Py_ssize_t left,
    Py_ssize_t top,
    Py_ssize_t out_w,
    Py_ssize_t out_h,
    int rotation_angle,
) noexcept nogil:
    cdef Py_ssize_t row_bytes = out_w * 4
    cdef Py_ssize_t row_stride_px = pitch // 4
    cdef Py_ssize_t tile = 32
    cdef Py_ssize_t y
    cdef Py_ssize_t x
    cdef Py_ssize_t dst_row_px
    cdef Py_ssize_t src_idx
    cdef Py_ssize_t src_x
    cdef Py_ssize_t n_blocks_y
    cdef Py_ssize_t n_blocks_x
    cdef Py_ssize_t block_y
    cdef Py_ssize_t block_x
    cdef Py_ssize_t by
    cdef Py_ssize_t bx
    cdef Py_ssize_t y_end
    cdef Py_ssize_t x_end
    cdef const uint32_t* src32 = <const uint32_t*>src_ptr
    cdef uint32_t* dst32 = <uint32_t*>dst_ptr

    if rotation_angle == 0:
        for y in range(out_h):
            memcpy(
                dst_ptr + (y * row_bytes),
                src_ptr + ((top + y) * pitch + left * 4),
                row_bytes,
            )
        return

    if rotation_angle == 180:
        for y in range(out_h):
            dst_row_px = y * out_w
            src_idx = (height - 1 - (top + y)) * row_stride_px + (width - 1 - left)
            for x in range(out_w):
                dst32[dst_row_px + x] = src32[src_idx - x]
        return

    if rotation_angle == 90:
        n_blocks_y = (out_h + tile - 1) // tile
        n_blocks_x = (out_w + tile - 1) // tile
        for block_y in range(n_blocks_y):
            by = block_y * tile
            y_end = by + tile
            if y_end > out_h:
                y_end = out_h
            for block_x in range(n_blocks_x):
                bx = block_x * tile
                x_end = bx + tile
                if x_end > out_w:
                    x_end = out_w
                for y in range(by, y_end):
                    dst_row_px = y * out_w
                    src_x = top + y
                    for x in range(bx, x_end):
                        dst32[dst_row_px + x] = src32[
                            (width - 1 - (left + x)) * row_stride_px + src_x
                        ]
        return

    n_blocks_y = (out_h + tile - 1) // tile
    n_blocks_x = (out_w + tile - 1) // tile
    for block_y in range(n_blocks_y):
        by = block_y * tile
        y_end = by + tile
        if y_end > out_h:
            y_end = out_h
        for block_x in range(n_blocks_x):
            bx = block_x * tile
            x_end = bx + tile
            if x_end > out_w:
                x_end = out_w
            for y in range(by, y_end):
                dst_row_px = y * out_w
                src_x = height - 1 - (top + y)
                for x in range(bx, x_end):
                    dst32[dst_row_px + x] = src32[(left + x) * row_stride_px + src_x]


cdef inline void _process_bgra_parallel_ptr(
    const uint8_t* src_ptr,
    uint8_t* dst_ptr,
    Py_ssize_t pitch,
    Py_ssize_t width,
    Py_ssize_t height,
    Py_ssize_t left,
    Py_ssize_t top,
    Py_ssize_t out_w,
    Py_ssize_t out_h,
    int rotation_angle,
) noexcept nogil:
    cdef Py_ssize_t row_bytes = out_w * 4
    cdef Py_ssize_t row_stride_px = pitch // 4
    cdef Py_ssize_t tile = 32
    cdef Py_ssize_t y
    cdef Py_ssize_t x
    cdef Py_ssize_t dst_row_px
    cdef Py_ssize_t src_idx
    cdef Py_ssize_t src_x
    cdef Py_ssize_t n_blocks_y
    cdef Py_ssize_t n_blocks_x
    cdef Py_ssize_t block_y
    cdef Py_ssize_t block_x
    cdef Py_ssize_t by
    cdef Py_ssize_t bx
    cdef Py_ssize_t y_end
    cdef Py_ssize_t x_end
    cdef const uint32_t* src32 = <const uint32_t*>src_ptr
    cdef uint32_t* dst32 = <uint32_t*>dst_ptr

    if rotation_angle == 0:
        for y in prange(out_h, schedule="static"):
            memcpy(
                dst_ptr + (y * row_bytes),
                src_ptr + ((top + y) * pitch + left * 4),
                row_bytes,
            )
        return

    if rotation_angle == 180:
        for y in prange(out_h, schedule="static"):
            dst_row_px = y * out_w
            src_idx = (height - 1 - (top + y)) * row_stride_px + (width - 1 - left)
            for x in range(out_w):
                dst32[dst_row_px + x] = src32[src_idx - x]
        return

    if rotation_angle == 90:
        n_blocks_y = (out_h + tile - 1) // tile
        n_blocks_x = (out_w + tile - 1) // tile
        for block_y in prange(n_blocks_y, schedule="static"):
            by = block_y * tile
            y_end = by + tile
            if y_end > out_h:
                y_end = out_h
            for block_x in range(n_blocks_x):
                bx = block_x * tile
                x_end = bx + tile
                if x_end > out_w:
                    x_end = out_w
                for y in range(by, y_end):
                    dst_row_px = y * out_w
                    src_x = top + y
                    for x in range(bx, x_end):
                        dst32[dst_row_px + x] = src32[
                            (width - 1 - (left + x)) * row_stride_px + src_x
                        ]
        return

    n_blocks_y = (out_h + tile - 1) // tile
    n_blocks_x = (out_w + tile - 1) // tile
    for block_y in prange(n_blocks_y, schedule="static"):
        by = block_y * tile
        y_end = by + tile
        if y_end > out_h:
            y_end = out_h
        for block_x in range(n_blocks_x):
            bx = block_x * tile
            x_end = bx + tile
            if x_end > out_w:
                x_end = out_w
            for y in range(by, y_end):
                dst_row_px = y * out_w
                src_x = height - 1 - (top + y)
                for x in range(bx, x_end):
                    dst32[dst_row_px + x] = src32[(left + x) * row_stride_px + src_x]


cdef inline void _process_rgb_serial_ptr(
    const uint8_t* src_ptr,
    uint8_t* dst_ptr,
    Py_ssize_t pitch,
    Py_ssize_t width,
    Py_ssize_t height,
    Py_ssize_t left,
    Py_ssize_t top,
    Py_ssize_t out_w,
    Py_ssize_t out_h,
    int rotation_angle,
) noexcept nogil:
    cdef Py_ssize_t y
    cdef Py_ssize_t x
    cdef Py_ssize_t dst_row
    cdef Py_ssize_t di
    cdef Py_ssize_t si
    cdef Py_ssize_t si_start
    cdef Py_ssize_t si_step

    for y in range(out_h):
        _source_step_params(
            y,
            width,
            height,
            left,
            top,
            pitch,
            rotation_angle,
            &si_start,
            &si_step,
        )
        dst_row = y * out_w * 3
        si = si_start
        for x in range(out_w):
            di = dst_row + x * 3
            dst_ptr[di] = src_ptr[si + 2]
            dst_ptr[di + 1] = src_ptr[si + 1]
            dst_ptr[di + 2] = src_ptr[si]
            si += si_step


cdef inline void _process_rgb_parallel_ptr(
    const uint8_t* src_ptr,
    uint8_t* dst_ptr,
    Py_ssize_t pitch,
    Py_ssize_t width,
    Py_ssize_t height,
    Py_ssize_t left,
    Py_ssize_t top,
    Py_ssize_t out_w,
    Py_ssize_t out_h,
    int rotation_angle,
) noexcept nogil:
    cdef Py_ssize_t y
    cdef Py_ssize_t x
    cdef Py_ssize_t src_base
    cdef Py_ssize_t dst_base
    cdef Py_ssize_t si
    cdef Py_ssize_t di

    if rotation_angle == 0:
        for y in prange(out_h, schedule="static"):
            src_base = (top + y) * pitch + left * 4
            dst_base = y * out_w * 3
            for x in range(out_w):
                si = src_base + x * 4
                di = dst_base + x * 3
                dst_ptr[di] = src_ptr[si + 2]
                dst_ptr[di + 1] = src_ptr[si + 1]
                dst_ptr[di + 2] = src_ptr[si]
        return

    if rotation_angle == 180:
        for y in prange(out_h, schedule="static"):
            for x in range(out_w):
                dst_ptr[(y * out_w + x) * 3] = src_ptr[((height - 1 - (top + y)) * pitch + (width - 1 - (left + x)) * 4) + 2]
                dst_ptr[(y * out_w + x) * 3 + 1] = src_ptr[((height - 1 - (top + y)) * pitch + (width - 1 - (left + x)) * 4) + 1]
                dst_ptr[(y * out_w + x) * 3 + 2] = src_ptr[((height - 1 - (top + y)) * pitch + (width - 1 - (left + x)) * 4)]
        return

    if rotation_angle == 90:
        for y in prange(out_h, schedule="static"):
            for x in range(out_w):
                dst_ptr[(y * out_w + x) * 3] = src_ptr[((width - 1 - (left + x)) * pitch + (top + y) * 4) + 2]
                dst_ptr[(y * out_w + x) * 3 + 1] = src_ptr[((width - 1 - (left + x)) * pitch + (top + y) * 4) + 1]
                dst_ptr[(y * out_w + x) * 3 + 2] = src_ptr[((width - 1 - (left + x)) * pitch + (top + y) * 4)]
        return

    for y in prange(out_h, schedule="static"):
        for x in range(out_w):
            dst_ptr[(y * out_w + x) * 3] = src_ptr[((left + x) * pitch + (height - 1 - (top + y)) * 4) + 2]
            dst_ptr[(y * out_w + x) * 3 + 1] = src_ptr[((left + x) * pitch + (height - 1 - (top + y)) * 4) + 1]
            dst_ptr[(y * out_w + x) * 3 + 2] = src_ptr[((left + x) * pitch + (height - 1 - (top + y)) * 4)]


cdef inline void _process_bgr_serial_ptr(
    const uint8_t* src_ptr,
    uint8_t* dst_ptr,
    Py_ssize_t pitch,
    Py_ssize_t width,
    Py_ssize_t height,
    Py_ssize_t left,
    Py_ssize_t top,
    Py_ssize_t out_w,
    Py_ssize_t out_h,
    int rotation_angle,
) noexcept nogil:
    cdef Py_ssize_t y
    cdef Py_ssize_t x
    cdef Py_ssize_t dst_row
    cdef Py_ssize_t di
    cdef Py_ssize_t si
    cdef Py_ssize_t si_start
    cdef Py_ssize_t si_step

    for y in range(out_h):
        _source_step_params(
            y,
            width,
            height,
            left,
            top,
            pitch,
            rotation_angle,
            &si_start,
            &si_step,
        )
        dst_row = y * out_w * 3
        si = si_start
        for x in range(out_w):
            di = dst_row + x * 3
            dst_ptr[di] = src_ptr[si]
            dst_ptr[di + 1] = src_ptr[si + 1]
            dst_ptr[di + 2] = src_ptr[si + 2]
            si += si_step


cdef inline void _process_bgr_parallel_ptr(
    const uint8_t* src_ptr,
    uint8_t* dst_ptr,
    Py_ssize_t pitch,
    Py_ssize_t width,
    Py_ssize_t height,
    Py_ssize_t left,
    Py_ssize_t top,
    Py_ssize_t out_w,
    Py_ssize_t out_h,
    int rotation_angle,
) noexcept nogil:
    cdef Py_ssize_t y
    cdef Py_ssize_t x

    if rotation_angle == 0:
        for y in prange(out_h, schedule="static"):
            for x in range(out_w):
                dst_ptr[(y * out_w + x) * 3] = src_ptr[((top + y) * pitch + (left + x) * 4)]
                dst_ptr[(y * out_w + x) * 3 + 1] = src_ptr[((top + y) * pitch + (left + x) * 4) + 1]
                dst_ptr[(y * out_w + x) * 3 + 2] = src_ptr[((top + y) * pitch + (left + x) * 4) + 2]
        return

    if rotation_angle == 180:
        for y in prange(out_h, schedule="static"):
            for x in range(out_w):
                dst_ptr[(y * out_w + x) * 3] = src_ptr[((height - 1 - (top + y)) * pitch + (width - 1 - (left + x)) * 4)]
                dst_ptr[(y * out_w + x) * 3 + 1] = src_ptr[((height - 1 - (top + y)) * pitch + (width - 1 - (left + x)) * 4) + 1]
                dst_ptr[(y * out_w + x) * 3 + 2] = src_ptr[((height - 1 - (top + y)) * pitch + (width - 1 - (left + x)) * 4) + 2]
        return

    if rotation_angle == 90:
        for y in prange(out_h, schedule="static"):
            for x in range(out_w):
                dst_ptr[(y * out_w + x) * 3] = src_ptr[((width - 1 - (left + x)) * pitch + (top + y) * 4)]
                dst_ptr[(y * out_w + x) * 3 + 1] = src_ptr[((width - 1 - (left + x)) * pitch + (top + y) * 4) + 1]
                dst_ptr[(y * out_w + x) * 3 + 2] = src_ptr[((width - 1 - (left + x)) * pitch + (top + y) * 4) + 2]
        return

    for y in prange(out_h, schedule="static"):
        for x in range(out_w):
            dst_ptr[(y * out_w + x) * 3] = src_ptr[((left + x) * pitch + (height - 1 - (top + y)) * 4)]
            dst_ptr[(y * out_w + x) * 3 + 1] = src_ptr[((left + x) * pitch + (height - 1 - (top + y)) * 4) + 1]
            dst_ptr[(y * out_w + x) * 3 + 2] = src_ptr[((left + x) * pitch + (height - 1 - (top + y)) * 4) + 2]


cdef inline void _process_rgba_serial_ptr(
    const uint8_t* src_ptr,
    uint8_t* dst_ptr,
    Py_ssize_t pitch,
    Py_ssize_t width,
    Py_ssize_t height,
    Py_ssize_t left,
    Py_ssize_t top,
    Py_ssize_t out_w,
    Py_ssize_t out_h,
    int rotation_angle,
) noexcept nogil:
    cdef Py_ssize_t y
    cdef Py_ssize_t x
    cdef Py_ssize_t dst_row
    cdef Py_ssize_t di
    cdef Py_ssize_t si
    cdef Py_ssize_t si_start
    cdef Py_ssize_t si_step

    for y in range(out_h):
        _source_step_params(
            y,
            width,
            height,
            left,
            top,
            pitch,
            rotation_angle,
            &si_start,
            &si_step,
        )
        dst_row = y * out_w * 4
        si = si_start
        for x in range(out_w):
            di = dst_row + x * 4
            dst_ptr[di] = src_ptr[si + 2]
            dst_ptr[di + 1] = src_ptr[si + 1]
            dst_ptr[di + 2] = src_ptr[si]
            dst_ptr[di + 3] = src_ptr[si + 3]
            si += si_step


cdef inline void _process_rgba_parallel_ptr(
    const uint8_t* src_ptr,
    uint8_t* dst_ptr,
    Py_ssize_t pitch,
    Py_ssize_t width,
    Py_ssize_t height,
    Py_ssize_t left,
    Py_ssize_t top,
    Py_ssize_t out_w,
    Py_ssize_t out_h,
    int rotation_angle,
) noexcept nogil:
    cdef Py_ssize_t y
    cdef Py_ssize_t x

    if rotation_angle == 0:
        for y in prange(out_h, schedule="static"):
            for x in range(out_w):
                dst_ptr[(y * out_w + x) * 4] = src_ptr[((top + y) * pitch + (left + x) * 4) + 2]
                dst_ptr[(y * out_w + x) * 4 + 1] = src_ptr[((top + y) * pitch + (left + x) * 4) + 1]
                dst_ptr[(y * out_w + x) * 4 + 2] = src_ptr[((top + y) * pitch + (left + x) * 4)]
                dst_ptr[(y * out_w + x) * 4 + 3] = src_ptr[((top + y) * pitch + (left + x) * 4) + 3]
        return

    if rotation_angle == 180:
        for y in prange(out_h, schedule="static"):
            for x in range(out_w):
                dst_ptr[(y * out_w + x) * 4] = src_ptr[((height - 1 - (top + y)) * pitch + (width - 1 - (left + x)) * 4) + 2]
                dst_ptr[(y * out_w + x) * 4 + 1] = src_ptr[((height - 1 - (top + y)) * pitch + (width - 1 - (left + x)) * 4) + 1]
                dst_ptr[(y * out_w + x) * 4 + 2] = src_ptr[((height - 1 - (top + y)) * pitch + (width - 1 - (left + x)) * 4)]
                dst_ptr[(y * out_w + x) * 4 + 3] = src_ptr[((height - 1 - (top + y)) * pitch + (width - 1 - (left + x)) * 4) + 3]
        return

    if rotation_angle == 90:
        for y in prange(out_h, schedule="static"):
            for x in range(out_w):
                dst_ptr[(y * out_w + x) * 4] = src_ptr[((width - 1 - (left + x)) * pitch + (top + y) * 4) + 2]
                dst_ptr[(y * out_w + x) * 4 + 1] = src_ptr[((width - 1 - (left + x)) * pitch + (top + y) * 4) + 1]
                dst_ptr[(y * out_w + x) * 4 + 2] = src_ptr[((width - 1 - (left + x)) * pitch + (top + y) * 4)]
                dst_ptr[(y * out_w + x) * 4 + 3] = src_ptr[((width - 1 - (left + x)) * pitch + (top + y) * 4) + 3]
        return

    for y in prange(out_h, schedule="static"):
        for x in range(out_w):
            dst_ptr[(y * out_w + x) * 4] = src_ptr[((left + x) * pitch + (height - 1 - (top + y)) * 4) + 2]
            dst_ptr[(y * out_w + x) * 4 + 1] = src_ptr[((left + x) * pitch + (height - 1 - (top + y)) * 4) + 1]
            dst_ptr[(y * out_w + x) * 4 + 2] = src_ptr[((left + x) * pitch + (height - 1 - (top + y)) * 4)]
            dst_ptr[(y * out_w + x) * 4 + 3] = src_ptr[((left + x) * pitch + (height - 1 - (top + y)) * 4) + 3]


cdef inline void _process_gray_serial_ptr(
    const uint8_t* src_ptr,
    uint8_t* dst_ptr,
    Py_ssize_t pitch,
    Py_ssize_t width,
    Py_ssize_t height,
    Py_ssize_t left,
    Py_ssize_t top,
    Py_ssize_t out_w,
    Py_ssize_t out_h,
    int rotation_angle,
) noexcept nogil:
    cdef Py_ssize_t y
    cdef Py_ssize_t x
    cdef Py_ssize_t di
    cdef Py_ssize_t si
    cdef Py_ssize_t si_start
    cdef Py_ssize_t si_step
    cdef uint32_t gray

    for y in range(out_h):
        _source_step_params(
            y,
            width,
            height,
            left,
            top,
            pitch,
            rotation_angle,
            &si_start,
            &si_step,
        )
        di = y * out_w
        si = si_start
        for x in range(out_w):
            gray = (
                9798 * <uint32_t>src_ptr[si + 2]
                + 19235 * <uint32_t>src_ptr[si + 1]
                + 3735 * <uint32_t>src_ptr[si]
                + 16384
            ) >> 15
            dst_ptr[di + x] = <uint8_t>gray
            si += si_step


cdef inline void _process_gray_parallel_ptr(
    const uint8_t* src_ptr,
    uint8_t* dst_ptr,
    Py_ssize_t pitch,
    Py_ssize_t width,
    Py_ssize_t height,
    Py_ssize_t left,
    Py_ssize_t top,
    Py_ssize_t out_w,
    Py_ssize_t out_h,
    int rotation_angle,
) noexcept nogil:
    cdef Py_ssize_t y
    cdef Py_ssize_t x
    cdef uint32_t gray

    if rotation_angle == 0:
        for y in prange(out_h, schedule="static"):
            for x in range(out_w):
                gray = (
                    9798 * <uint32_t>src_ptr[((top + y) * pitch + (left + x) * 4) + 2]
                    + 19235 * <uint32_t>src_ptr[((top + y) * pitch + (left + x) * 4) + 1]
                    + 3735 * <uint32_t>src_ptr[((top + y) * pitch + (left + x) * 4)]
                    + 16384
                ) >> 15
                dst_ptr[y * out_w + x] = <uint8_t>gray
        return

    if rotation_angle == 180:
        for y in prange(out_h, schedule="static"):
            for x in range(out_w):
                gray = (
                    9798 * <uint32_t>src_ptr[((height - 1 - (top + y)) * pitch + (width - 1 - (left + x)) * 4) + 2]
                    + 19235 * <uint32_t>src_ptr[((height - 1 - (top + y)) * pitch + (width - 1 - (left + x)) * 4) + 1]
                    + 3735 * <uint32_t>src_ptr[((height - 1 - (top + y)) * pitch + (width - 1 - (left + x)) * 4)]
                    + 16384
                ) >> 15
                dst_ptr[y * out_w + x] = <uint8_t>gray
        return

    if rotation_angle == 90:
        for y in prange(out_h, schedule="static"):
            for x in range(out_w):
                gray = (
                    9798 * <uint32_t>src_ptr[((width - 1 - (left + x)) * pitch + (top + y) * 4) + 2]
                    + 19235 * <uint32_t>src_ptr[((width - 1 - (left + x)) * pitch + (top + y) * 4) + 1]
                    + 3735 * <uint32_t>src_ptr[((width - 1 - (left + x)) * pitch + (top + y) * 4)]
                    + 16384
                ) >> 15
                dst_ptr[y * out_w + x] = <uint8_t>gray
        return

    for y in prange(out_h, schedule="static"):
        for x in range(out_w):
            gray = (
                9798 * <uint32_t>src_ptr[((left + x) * pitch + (height - 1 - (top + y)) * 4) + 2]
                + 19235 * <uint32_t>src_ptr[((left + x) * pitch + (height - 1 - (top + y)) * 4) + 1]
                + 3735 * <uint32_t>src_ptr[((left + x) * pitch + (height - 1 - (top + y)) * 4)]
                + 16384
            ) >> 15
            dst_ptr[y * out_w + x] = <uint8_t>gray


def process_bgra_into(
    const uint8_t[::1] src,
    int pitch,
    int width,
    int height,
    region,
    int rotation_angle,
    str mode,
    uint8_t[::1] dst,
) -> None:
    """Rotate/crop/convert BGRA source bytes into contiguous destination bytes."""
    cdef Py_ssize_t left
    cdef Py_ssize_t top
    cdef Py_ssize_t right
    cdef Py_ssize_t bottom
    cdef Py_ssize_t out_w
    cdef Py_ssize_t out_h
    cdef Py_ssize_t expected_rows
    cdef Py_ssize_t expected_active_cols
    cdef Py_ssize_t expected_dst_size
    cdef Py_ssize_t n_pixels
    cdef int mode_code
    cdef Py_ssize_t channels
    cdef bint use_parallel
    cdef Py_ssize_t pitch_sz
    cdef Py_ssize_t width_sz
    cdef Py_ssize_t height_sz
    cdef const uint8_t* src_ptr
    cdef uint8_t* dst_ptr

    mode_code = _mode_to_code(mode)
    _validate_rotation_angle(rotation_angle)

    if width <= 0 or height <= 0:
        raise ValueError("width and height must be > 0")
    if pitch <= 0 or (pitch % 4) != 0:
        raise ValueError(f"Invalid BGRA pitch: {pitch}")

    width_sz = <Py_ssize_t>width
    height_sz = <Py_ssize_t>height
    pitch_sz = <Py_ssize_t>pitch

    left = <Py_ssize_t>region[0]
    top = <Py_ssize_t>region[1]
    right = <Py_ssize_t>region[2]
    bottom = <Py_ssize_t>region[3]

    if not (0 <= left < right <= width_sz and 0 <= top < bottom <= height_sz):
        raise ValueError(
            f"Invalid region {tuple(region)} for frame size {width}x{height}."
        )

    if rotation_angle == 0 or rotation_angle == 180:
        expected_rows = height_sz
        expected_active_cols = width_sz
    else:
        expected_rows = width_sz
        expected_active_cols = height_sz
    if pitch_sz < expected_active_cols * 4:
        raise ValueError(
            f"Source pitch {pitch} smaller than required active bytes "
            f"{expected_active_cols * 4}."
        )
    if src.shape[0] < expected_rows * pitch_sz:
        raise ValueError(
            f"Source byte size {src.shape[0]} smaller than required "
            f"{expected_rows * pitch}."
        )

    out_w = right - left
    out_h = bottom - top
    if mode_code == MODE_GRAY:
        channels = 1
    elif mode_code == MODE_RGB or mode_code == MODE_BGR:
        channels = 3
    else:
        channels = 4

    expected_dst_size = out_w * out_h * channels
    if dst.shape[0] != expected_dst_size:
        raise ValueError(
            f"Destination size mismatch: expected {expected_dst_size}, got {dst.shape[0]}."
        )

    src_ptr = &src[0]
    dst_ptr = &dst[0]
    n_pixels = out_w * out_h
    use_parallel = n_pixels >= _PARALLEL_PIXELS_THRESHOLD

    with nogil:
        if mode_code == MODE_BGRA:
            if use_parallel:
                _process_bgra_parallel_ptr(
                    src_ptr,
                    dst_ptr,
                    pitch_sz,
                    width_sz,
                    height_sz,
                    left,
                    top,
                    out_w,
                    out_h,
                    rotation_angle,
                )
            else:
                _process_bgra_serial_ptr(
                    src_ptr,
                    dst_ptr,
                    pitch_sz,
                    width_sz,
                    height_sz,
                    left,
                    top,
                    out_w,
                    out_h,
                    rotation_angle,
                )
        elif mode_code == MODE_RGB:
            if use_parallel:
                _process_rgb_parallel_ptr(
                    src_ptr,
                    dst_ptr,
                    pitch_sz,
                    width_sz,
                    height_sz,
                    left,
                    top,
                    out_w,
                    out_h,
                    rotation_angle,
                )
            else:
                _process_rgb_serial_ptr(
                    src_ptr,
                    dst_ptr,
                    pitch_sz,
                    width_sz,
                    height_sz,
                    left,
                    top,
                    out_w,
                    out_h,
                    rotation_angle,
                )
        elif mode_code == MODE_BGR:
            if use_parallel:
                _process_bgr_parallel_ptr(
                    src_ptr,
                    dst_ptr,
                    pitch_sz,
                    width_sz,
                    height_sz,
                    left,
                    top,
                    out_w,
                    out_h,
                    rotation_angle,
                )
            else:
                _process_bgr_serial_ptr(
                    src_ptr,
                    dst_ptr,
                    pitch_sz,
                    width_sz,
                    height_sz,
                    left,
                    top,
                    out_w,
                    out_h,
                    rotation_angle,
                )
        elif mode_code == MODE_RGBA:
            if use_parallel:
                _process_rgba_parallel_ptr(
                    src_ptr,
                    dst_ptr,
                    pitch_sz,
                    width_sz,
                    height_sz,
                    left,
                    top,
                    out_w,
                    out_h,
                    rotation_angle,
                )
            else:
                _process_rgba_serial_ptr(
                    src_ptr,
                    dst_ptr,
                    pitch_sz,
                    width_sz,
                    height_sz,
                    left,
                    top,
                    out_w,
                    out_h,
                    rotation_angle,
                )
        else:
            if use_parallel:
                _process_gray_parallel_ptr(
                    src_ptr,
                    dst_ptr,
                    pitch_sz,
                    width_sz,
                    height_sz,
                    left,
                    top,
                    out_w,
                    out_h,
                    rotation_angle,
                )
            else:
                _process_gray_serial_ptr(
                    src_ptr,
                    dst_ptr,
                    pitch_sz,
                    width_sz,
                    height_sz,
                    left,
                    top,
                    out_w,
                    out_h,
                    rotation_angle,
                )
