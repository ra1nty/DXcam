"""Win32 helpers for resolving process/window handles.

Use with ``target_hwnd`` for WinRT window capture:
    >>> from dxcam.util.hwnd import enumerate_hwnds_for_pid, pick_largest_visible_hwnd
    >>> hwnd = pick_largest_visible_hwnd(pid=1234)
    >>> if hwnd:
    ...     cam = dxcam.create(backend="winrt", target_hwnd=hwnd)
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
user32 = ctypes.windll.user32


def is_window_valid(hwnd: int) -> bool:
    """Return True if the given handle is a valid, existing window."""
    return bool(user32.IsWindow(hwnd))


def enumerate_hwnds_for_pid(pid: int) -> list[int]:
    """Enumerate top-level window handles owned by the given process.

    Args:
        pid: Process ID (e.g. from ``os.getpid()`` or another process).

    Returns:
        List of HWND values for visible top-level windows belonging to the process.
    """
    found: list[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def enum_proc(hwnd: int, _lparam: int) -> bool:
        window_pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(window_pid))
        if window_pid.value == pid and user32.IsWindowVisible(hwnd):
            found.append(int(hwnd))
        return True

    user32.EnumWindows(enum_proc, 0)
    return found


def pick_largest_visible_hwnd(pid: int) -> int | None:
    """Pick the largest visible top-level window for the given process.

    Uses client-area size as the heuristic. Returns ``None`` if no windows
    are found.

    Args:
        pid: Process ID.

    Returns:
        HWND of the largest window, or ``None``.
    """
    hwnds = enumerate_hwnds_for_pid(pid)
    if not hwnds:
        return None

    rect = wintypes.RECT()
    best_hwnd: int | None = None
    best_area = 0

    for hwnd in hwnds:
        if user32.GetClientRect(hwnd, ctypes.byref(rect)):
            w = rect.right - rect.left
            h = rect.bottom - rect.top
            if w > 0 and h > 0:
                area = w * h
                if area > best_area:
                    best_area = area
                    best_hwnd = hwnd

    return best_hwnd
