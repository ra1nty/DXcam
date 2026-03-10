# cython: language_level=3
# cython: boundscheck=False
# cython: wraparound=False
# cython: initializedcheck=False
# cython: nonecheck=False
# cython: cdivision=True

from libc.stdint cimport uint8_t, uint32_t
from cython.parallel cimport prange
cimport numpy as cnp
import numpy as np
import os

cnp.import_array()

# Default to enabling OpenMP for frames at or above 128x128.
# The crossover point is typically far below HD on modern CPUs.
cdef Py_ssize_t _DEFAULT_PARALLEL_PIXELS_THRESHOLD = 128 * 128
cdef Py_ssize_t _PARALLEL_PIXELS_THRESHOLD = _DEFAULT_PARALLEL_PIXELS_THRESHOLD
# Tile size used by rotate(90/270) transpose-like loops.
cdef Py_ssize_t _DEFAULT_ROTATE_TILE = 32
cdef Py_ssize_t _ROTATE_TILE = _DEFAULT_ROTATE_TILE


def get_parallel_pixels_threshold() -> int:
    """Return current pixel-count threshold used to enable OpenMP paths."""
    return int(_PARALLEL_PIXELS_THRESHOLD)


def set_parallel_pixels_threshold(value: int) -> None:
    """Set pixel-count threshold used to enable OpenMP paths.

    A value of ``0`` forces parallel path for all frame sizes.
    """
    global _PARALLEL_PIXELS_THRESHOLD
    if value < 0:
        raise ValueError("parallel threshold must be >= 0")
    _PARALLEL_PIXELS_THRESHOLD = <Py_ssize_t>value


def reset_parallel_pixels_threshold() -> None:
    """Reset OpenMP threshold to the built-in default."""
    global _PARALLEL_PIXELS_THRESHOLD
    _PARALLEL_PIXELS_THRESHOLD = _DEFAULT_PARALLEL_PIXELS_THRESHOLD


def get_rotate_tile() -> int:
    """Return current tile size used by rotate(90/270) kernels."""
    return int(_ROTATE_TILE)


def set_rotate_tile(value: int) -> None:
    """Set tile size used by rotate(90/270) kernels.

    Valid range is 4..256 pixels per tile edge.
    """
    global _ROTATE_TILE
    if value < 4 or value > 256:
        raise ValueError("rotate tile must be in range [4, 256]")
    _ROTATE_TILE = <Py_ssize_t>value


def reset_rotate_tile() -> None:
    """Reset rotate tile size to the built-in default."""
    global _ROTATE_TILE
    _ROTATE_TILE = _DEFAULT_ROTATE_TILE


def _initialize_tuning_from_env() -> None:
    raw = os.environ.get("DXCAM_NUMPY_PARALLEL_THRESHOLD")
    if raw is not None:
        try:
            value = int(raw)
        except ValueError:
            value = -1
        if value >= 0:
            set_parallel_pixels_threshold(value)

    raw = os.environ.get("DXCAM_NUMPY_ROTATE_TILE")
    if raw is not None:
        try:
            value = int(raw)
        except ValueError:
            value = -1
        if value >= 4:
            try:
                set_rotate_tile(value)
            except ValueError:
                pass


_initialize_tuning_from_env()


cdef enum _ModeCode:
    MODE_RGB = 1
    MODE_BGR = 2
    MODE_RGBA = 3
    MODE_GRAY = 4


cdef inline int _mode_to_code(str mode) except -1:
    if mode == "RGB":
        return MODE_RGB
    if mode == "BGR":
        return MODE_BGR
    if mode == "RGBA":
        return MODE_RGBA
    if mode == "GRAY":
        return MODE_GRAY
    raise ValueError(
        f"Unsupported output mode '{mode}'. Supported modes: RGB, BGR, RGBA, GRAY."
    )


cdef inline cnp.ndarray[uint8_t, ndim=3, mode="c"] _ensure_src_bgra_contiguous(
    cnp.ndarray[uint8_t, ndim=3] src
):
    if src.shape[2] != 4:
        raise ValueError(f"Expected BGRA input with 4 channels, got {src.shape[2]}.")
    if src.flags.c_contiguous:
        return src
    return np.ascontiguousarray(src, dtype=np.uint8)


cdef inline cnp.ndarray[uint8_t, ndim=3, mode="c"] _ensure_dst_contiguous(
    cnp.ndarray[uint8_t, ndim=3] dst
):
    if not dst.flags.c_contiguous:
        raise ValueError("Destination array must be C-contiguous.")
    return dst


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


cdef inline void _copy_bgra_prepare_serial(
    const uint32_t[:, :] src32,
    uint32_t[:, :] dst32,
    Py_ssize_t width,
    Py_ssize_t height,
    Py_ssize_t left,
    Py_ssize_t top,
    int rotation_angle,
) noexcept nogil:
    cdef Py_ssize_t out_h = dst32.shape[0]
    cdef Py_ssize_t out_w = dst32.shape[1]
    cdef Py_ssize_t tile = _ROTATE_TILE
    cdef Py_ssize_t n_blocks_y
    cdef Py_ssize_t n_blocks_x
    cdef Py_ssize_t block_y
    cdef Py_ssize_t block_x
    cdef Py_ssize_t by
    cdef Py_ssize_t bx
    cdef Py_ssize_t y_end
    cdef Py_ssize_t x_end
    cdef Py_ssize_t y
    cdef Py_ssize_t x
    cdef Py_ssize_t src_y
    cdef Py_ssize_t src_x

    if rotation_angle == 0:
        for y in range(out_h):
            src_y = top + y
            for x in range(out_w):
                src_x = left + x
                dst32[y, x] = src32[src_y, src_x]
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
                    src_x = top + y
                    for x in range(bx, x_end):
                        src_y = width - 1 - (left + x)
                        dst32[y, x] = src32[src_y, src_x]
        return
    if rotation_angle == 180:
        for y in range(out_h):
            src_y = height - 1 - (top + y)
            for x in range(out_w):
                src_x = width - 1 - (left + x)
                dst32[y, x] = src32[src_y, src_x]
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
                src_x = height - 1 - (top + y)
                for x in range(bx, x_end):
                    src_y = left + x
                    dst32[y, x] = src32[src_y, src_x]


cdef inline void _copy_bgra_prepare_parallel(
    const uint32_t[:, :] src32,
    uint32_t[:, :] dst32,
    Py_ssize_t width,
    Py_ssize_t height,
    Py_ssize_t left,
    Py_ssize_t top,
    int rotation_angle,
) noexcept nogil:
    cdef Py_ssize_t out_h = dst32.shape[0]
    cdef Py_ssize_t out_w = dst32.shape[1]
    cdef Py_ssize_t tile = _ROTATE_TILE
    cdef Py_ssize_t n_blocks_y
    cdef Py_ssize_t n_blocks_x
    cdef Py_ssize_t block_y
    cdef Py_ssize_t block_x
    cdef Py_ssize_t by
    cdef Py_ssize_t bx
    cdef Py_ssize_t y_end
    cdef Py_ssize_t x_end
    cdef Py_ssize_t y
    cdef Py_ssize_t x
    cdef Py_ssize_t src_y
    cdef Py_ssize_t src_x

    if rotation_angle == 0:
        for y in prange(out_h, schedule="static"):
            src_y = top + y
            for x in range(out_w):
                src_x = left + x
                dst32[y, x] = src32[src_y, src_x]
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
                    src_x = top + y
                    for x in range(bx, x_end):
                        src_y = width - 1 - (left + x)
                        dst32[y, x] = src32[src_y, src_x]
        return
    if rotation_angle == 180:
        for y in prange(out_h, schedule="static"):
            src_y = height - 1 - (top + y)
            for x in range(out_w):
                src_x = width - 1 - (left + x)
                dst32[y, x] = src32[src_y, src_x]
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
                src_x = height - 1 - (top + y)
                for x in range(bx, x_end):
                    src_y = left + x
                    dst32[y, x] = src32[src_y, src_x]


def prepare_bgra(
    cnp.ndarray[uint8_t, ndim=3] src,
    int width,
    int height,
    region,
    int rotation_angle,
) -> cnp.ndarray:
    """Map/rotate/crop mapped BGRA image into contiguous BGRA output."""
    cdef Py_ssize_t left
    cdef Py_ssize_t top
    cdef Py_ssize_t right
    cdef Py_ssize_t bottom
    cdef Py_ssize_t out_h
    cdef Py_ssize_t out_w
    cdef cnp.ndarray[uint8_t, ndim=3, mode="c"] out

    if src.shape[2] != 4:
        raise ValueError(f"Expected BGRA source with 4 channels, got {src.shape[2]}.")
    _validate_rotation_angle(rotation_angle)

    left = <Py_ssize_t>region[0]
    top = <Py_ssize_t>region[1]
    right = <Py_ssize_t>region[2]
    bottom = <Py_ssize_t>region[3]
    out_w = right - left
    out_h = bottom - top
    out = np.empty((out_h, out_w, 4), dtype=np.uint8)
    prepare_bgra_into(src, out, width, height, region, rotation_angle)
    return out


def prepare_bgra_into(
    cnp.ndarray[uint8_t, ndim=3] src,
    cnp.ndarray[uint8_t, ndim=3] dst,
    int width,
    int height,
    region,
    int rotation_angle,
) -> None:
    """Map/rotate/crop mapped BGRA ``src`` into caller-provided contiguous ``dst``."""
    cdef cnp.ndarray[uint8_t, ndim=3, mode="c"] src_c = _ensure_src_bgra_contiguous(src)
    cdef cnp.ndarray[uint8_t, ndim=3, mode="c"] dst_c = _ensure_dst_contiguous(dst)
    cdef cnp.ndarray[uint32_t, ndim=2, mode="c"] src32_nd
    cdef cnp.ndarray[uint32_t, ndim=2, mode="c"] dst32_nd
    cdef const uint32_t[:, :] src32_view
    cdef uint32_t[:, :] dst32_view
    cdef Py_ssize_t left
    cdef Py_ssize_t top
    cdef Py_ssize_t right
    cdef Py_ssize_t bottom
    cdef Py_ssize_t out_h
    cdef Py_ssize_t out_w
    cdef Py_ssize_t expected_src_rows
    cdef Py_ssize_t expected_active_cols
    cdef Py_ssize_t n_pixels
    cdef bint use_parallel

    if src_c.shape[2] != 4:
        raise ValueError(f"Expected BGRA source with 4 channels, got {src_c.shape[2]}.")
    if dst_c.shape[2] != 4:
        raise ValueError(
            f"BGRA destination must have 4 channels, got {dst_c.shape[2]}."
        )

    _validate_rotation_angle(rotation_angle)
    left = <Py_ssize_t>region[0]
    top = <Py_ssize_t>region[1]
    right = <Py_ssize_t>region[2]
    bottom = <Py_ssize_t>region[3]

    if width <= 0 or height <= 0:
        raise ValueError("width and height must be > 0")
    if not (0 <= left < right <= width and 0 <= top < bottom <= height):
        raise ValueError(
            f"Invalid region {tuple(region)} for frame size {width}x{height}."
        )

    if rotation_angle == 0 or rotation_angle == 180:
        expected_src_rows = height
        expected_active_cols = width
    else:
        expected_src_rows = width
        expected_active_cols = height

    if src_c.shape[0] != expected_src_rows:
        raise ValueError(
            f"Unexpected source rows for rotation={rotation_angle}: "
            f"expected {expected_src_rows}, got {src_c.shape[0]}."
        )
    if src_c.shape[1] < expected_active_cols:
        raise ValueError(
            f"Source pitch columns {src_c.shape[1]} smaller than required "
            f"{expected_active_cols}."
        )

    out_w = right - left
    out_h = bottom - top
    if dst_c.shape[0] != out_h or dst_c.shape[1] != out_w:
        raise ValueError(
            "Destination shape does not match requested region: "
            f"region=({left}, {top}, {right}, {bottom}) "
            f"dst=({dst_c.shape[0]}, {dst_c.shape[1]}, {dst_c.shape[2]})."
        )

    src32_nd = np.asarray(src_c).view(np.uint32).reshape(src_c.shape[0], src_c.shape[1])
    dst32_nd = np.asarray(dst_c).view(np.uint32).reshape(dst_c.shape[0], dst_c.shape[1])
    src32_view = src32_nd
    dst32_view = dst32_nd

    n_pixels = out_w * out_h
    use_parallel = n_pixels >= _PARALLEL_PIXELS_THRESHOLD
    with nogil:
        if use_parallel:
            _copy_bgra_prepare_parallel(
                src32_view,
                dst32_view,
                width,
                height,
                left,
                top,
                rotation_angle,
            )
        else:
            _copy_bgra_prepare_serial(
                src32_view,
                dst32_view,
                width,
                height,
                left,
                top,
                rotation_angle,
            )


cdef inline void _bgra_to_rgb_ptr(
    const uint8_t* src,
    uint8_t* dst,
    Py_ssize_t n_pixels,
) noexcept nogil:
    cdef Py_ssize_t i
    for i in range(n_pixels):
        dst[0] = src[2]
        dst[1] = src[1]
        dst[2] = src[0]
        src += 4
        dst += 3


cdef inline void _bgra_to_rgb_ptr_parallel(
    const uint8_t* src,
    uint8_t* dst,
    Py_ssize_t h,
    Py_ssize_t w,
) noexcept nogil:
    cdef Py_ssize_t y
    cdef Py_ssize_t x
    cdef Py_ssize_t src_row_stride = w * 4
    cdef Py_ssize_t dst_row_stride = w * 3
    cdef Py_ssize_t src_base
    cdef Py_ssize_t dst_base
    cdef Py_ssize_t si
    cdef Py_ssize_t di
    for y in prange(h, schedule="static"):
        src_base = y * src_row_stride
        dst_base = y * dst_row_stride
        for x in range(w):
            si = src_base + (x * 4)
            di = dst_base + (x * 3)
            dst[di] = src[si + 2]
            dst[di + 1] = src[si + 1]
            dst[di + 2] = src[si]


cdef inline void _bgra_to_bgr_ptr(
    const uint8_t* src,
    uint8_t* dst,
    Py_ssize_t n_pixels,
) noexcept nogil:
    cdef Py_ssize_t i
    for i in range(n_pixels):
        dst[0] = src[0]
        dst[1] = src[1]
        dst[2] = src[2]
        src += 4
        dst += 3


cdef inline void _bgra_to_bgr_ptr_parallel(
    const uint8_t* src,
    uint8_t* dst,
    Py_ssize_t h,
    Py_ssize_t w,
) noexcept nogil:
    cdef Py_ssize_t y
    cdef Py_ssize_t x
    cdef Py_ssize_t src_row_stride = w * 4
    cdef Py_ssize_t dst_row_stride = w * 3
    cdef Py_ssize_t src_base
    cdef Py_ssize_t dst_base
    cdef Py_ssize_t si
    cdef Py_ssize_t di
    for y in prange(h, schedule="static"):
        src_base = y * src_row_stride
        dst_base = y * dst_row_stride
        for x in range(w):
            si = src_base + (x * 4)
            di = dst_base + (x * 3)
            dst[di] = src[si]
            dst[di + 1] = src[si + 1]
            dst[di + 2] = src[si + 2]


cdef inline void _bgra_to_rgba_ptr(
    const uint8_t* src,
    uint8_t* dst,
    Py_ssize_t n_pixels,
) noexcept nogil:
    cdef Py_ssize_t i
    for i in range(n_pixels):
        dst[0] = src[2]
        dst[1] = src[1]
        dst[2] = src[0]
        dst[3] = src[3]
        src += 4
        dst += 4


cdef inline void _bgra_to_rgba_ptr_parallel(
    const uint8_t* src,
    uint8_t* dst,
    Py_ssize_t h,
    Py_ssize_t w,
) noexcept nogil:
    cdef Py_ssize_t y
    cdef Py_ssize_t x
    cdef Py_ssize_t src_row_stride = w * 4
    cdef Py_ssize_t dst_row_stride = w * 4
    cdef Py_ssize_t src_base
    cdef Py_ssize_t dst_base
    cdef Py_ssize_t si
    cdef Py_ssize_t di
    for y in prange(h, schedule="static"):
        src_base = y * src_row_stride
        dst_base = y * dst_row_stride
        for x in range(w):
            si = src_base + (x * 4)
            di = dst_base + (x * 4)
            dst[di] = src[si + 2]
            dst[di + 1] = src[si + 1]
            dst[di + 2] = src[si]
            dst[di + 3] = src[si + 3]


cdef inline void _bgra_to_gray_ptr(
    const uint8_t* src,
    uint8_t* dst,
    Py_ssize_t n_pixels,
) noexcept nogil:
    cdef Py_ssize_t i
    cdef uint32_t gray
    for i in range(n_pixels):
        gray = (
            9798 * <uint32_t>src[2]
            + 19235 * <uint32_t>src[1]
            + 3735 * <uint32_t>src[0]
            + 16384
        ) >> 15
        dst[0] = <uint8_t>gray
        src += 4
        dst += 1


cdef inline void _bgra_to_gray_ptr_parallel(
    const uint8_t* src,
    uint8_t* dst,
    Py_ssize_t h,
    Py_ssize_t w,
) noexcept nogil:
    cdef Py_ssize_t y
    cdef Py_ssize_t x
    cdef Py_ssize_t src_row_stride = w * 4
    cdef Py_ssize_t dst_row_stride = w
    cdef Py_ssize_t src_base
    cdef Py_ssize_t dst_base
    cdef Py_ssize_t si
    cdef Py_ssize_t di
    cdef uint32_t gray
    for y in prange(h, schedule="static"):
        src_base = y * src_row_stride
        dst_base = y * dst_row_stride
        for x in range(w):
            si = src_base + (x * 4)
            di = dst_base + x
            gray = (
                9798 * <uint32_t>src[si + 2]
                + 19235 * <uint32_t>src[si + 1]
                + 3735 * <uint32_t>src[si]
                + 16384
            ) >> 15
            dst[di] = <uint8_t>gray


def convert_bgra(
    cnp.ndarray[uint8_t, ndim=3] src,
    mode: str,
) -> cnp.ndarray:
    """Convert BGRA ``src`` to a target color mode."""
    cdef cnp.ndarray[uint8_t, ndim=3, mode="c"] src_c = _ensure_src_bgra_contiguous(src)
    cdef Py_ssize_t h = src_c.shape[0]
    cdef Py_ssize_t w = src_c.shape[1]
    cdef Py_ssize_t n_pixels = h * w
    cdef int mode_code = _mode_to_code(mode)
    cdef cnp.ndarray[uint8_t, ndim=3, mode="c"] out
    cdef const uint8_t* src_ptr = <const uint8_t*>src_c.data
    cdef uint8_t* dst_ptr
    cdef bint use_parallel = n_pixels >= _PARALLEL_PIXELS_THRESHOLD

    if mode_code == MODE_RGB:
        out = np.empty((h, w, 3), dtype=np.uint8)
        dst_ptr = <uint8_t*>out.data
        with nogil:
            if use_parallel:
                _bgra_to_rgb_ptr_parallel(src_ptr, dst_ptr, h, w)
            else:
                _bgra_to_rgb_ptr(src_ptr, dst_ptr, n_pixels)
        return out
    if mode_code == MODE_BGR:
        out = np.empty((h, w, 3), dtype=np.uint8)
        dst_ptr = <uint8_t*>out.data
        with nogil:
            if use_parallel:
                _bgra_to_bgr_ptr_parallel(src_ptr, dst_ptr, h, w)
            else:
                _bgra_to_bgr_ptr(src_ptr, dst_ptr, n_pixels)
        return out
    if mode_code == MODE_RGBA:
        out = np.empty((h, w, 4), dtype=np.uint8)
        dst_ptr = <uint8_t*>out.data
        with nogil:
            if use_parallel:
                _bgra_to_rgba_ptr_parallel(src_ptr, dst_ptr, h, w)
            else:
                _bgra_to_rgba_ptr(src_ptr, dst_ptr, n_pixels)
        return out

    out = np.empty((h, w, 1), dtype=np.uint8)
    dst_ptr = <uint8_t*>out.data
    with nogil:
        if use_parallel:
            _bgra_to_gray_ptr_parallel(src_ptr, dst_ptr, h, w)
        else:
            _bgra_to_gray_ptr(src_ptr, dst_ptr, n_pixels)
    return out


def convert_bgra_into(
    cnp.ndarray[uint8_t, ndim=3] src,
    cnp.ndarray[uint8_t, ndim=3] dst,
    mode: str,
) -> None:
    """Convert BGRA ``src`` into caller-provided ``dst`` array."""
    cdef cnp.ndarray[uint8_t, ndim=3, mode="c"] src_c = _ensure_src_bgra_contiguous(src)
    cdef cnp.ndarray[uint8_t, ndim=3, mode="c"] dst_c = _ensure_dst_contiguous(dst)
    cdef Py_ssize_t src_h = src_c.shape[0]
    cdef Py_ssize_t src_w = src_c.shape[1]
    cdef Py_ssize_t dst_h = dst_c.shape[0]
    cdef Py_ssize_t dst_w = dst_c.shape[1]
    cdef int mode_code = _mode_to_code(mode)
    cdef Py_ssize_t n_pixels = src_h * src_w
    cdef const uint8_t* src_ptr
    cdef uint8_t* dst_ptr
    cdef bint use_parallel = n_pixels >= _PARALLEL_PIXELS_THRESHOLD

    if dst_h != src_h or dst_w != src_w:
        raise ValueError(
            "Destination shape does not match source dimensions: "
            f"src=({src_h}, {src_w}, {src_c.shape[2]}) "
            f"dst=({dst_h}, {dst_w}, {dst_c.shape[2]})."
        )

    src_ptr = <const uint8_t*>src_c.data
    dst_ptr = <uint8_t*>dst_c.data

    if mode_code == MODE_RGB:
        if dst_c.shape[2] != 3:
            raise ValueError(
                f"RGB destination must have 3 channels, got {dst_c.shape[2]}."
            )
        with nogil:
            if use_parallel:
                _bgra_to_rgb_ptr_parallel(src_ptr, dst_ptr, src_h, src_w)
            else:
                _bgra_to_rgb_ptr(src_ptr, dst_ptr, n_pixels)
        return
    if mode_code == MODE_BGR:
        if dst_c.shape[2] != 3:
            raise ValueError(
                f"BGR destination must have 3 channels, got {dst_c.shape[2]}."
            )
        with nogil:
            if use_parallel:
                _bgra_to_bgr_ptr_parallel(src_ptr, dst_ptr, src_h, src_w)
            else:
                _bgra_to_bgr_ptr(src_ptr, dst_ptr, n_pixels)
        return
    if mode_code == MODE_RGBA:
        if dst_c.shape[2] != 4:
            raise ValueError(
                f"RGBA destination must have 4 channels, got {dst_c.shape[2]}."
            )
        with nogil:
            if use_parallel:
                _bgra_to_rgba_ptr_parallel(src_ptr, dst_ptr, src_h, src_w)
            else:
                _bgra_to_rgba_ptr(src_ptr, dst_ptr, n_pixels)
        return

    if dst_c.shape[2] != 1:
        raise ValueError(f"GRAY destination must have 1 channel, got {dst_c.shape[2]}.")
    with nogil:
        if use_parallel:
            _bgra_to_gray_ptr_parallel(src_ptr, dst_ptr, src_h, src_w)
        else:
            _bgra_to_gray_ptr(src_ptr, dst_ptr, n_pixels)
