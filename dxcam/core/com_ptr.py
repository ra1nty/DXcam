from __future__ import annotations

import ctypes
from typing import Any


def clear_com_pointer(pointer: Any) -> None:
    """Set a COM pointer value to NULL in-place without calling Release()."""
    if pointer is None:
        return
    try:
        raw = ctypes.cast(ctypes.byref(pointer), ctypes.POINTER(ctypes.c_void_p))
        raw[0] = None
    except Exception:
        # Best-effort cleanup helper; callers may still overwrite the attribute.
        return


def release_com_pointer(pointer: Any) -> None:
    """Release a COM pointer once, then null it to avoid late double-Release."""
    if pointer is None or not pointer:
        return
    try:
        pointer.Release()
    finally:
        clear_com_pointer(pointer)
