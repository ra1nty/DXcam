"""A bounded, non-activating GDI workload for compare_capture.py.

Only this process's own window is drawn. No desktop pixels are read here.
"""

from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import json
import sys
import threading
import time

MARKER = (1, 0, 1, 0, 0, 1, 0, 1)
CELL = 8
BAR_X = 20
BAR_Y = 20


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument("--fps", type=float, default=120)
    parser.add_argument("--max-seconds", type=float, default=180)
    args = parser.parse_args()
    if sys.platform != "win32":
        raise RuntimeError("The controlled workload requires Windows.")
    user32, gdi32, kernel32 = ctypes.windll.user32, ctypes.windll.gdi32, ctypes.windll.kernel32
    try:
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except AttributeError:
        user32.SetProcessDPIAware()
    width = min(args.width, user32.GetSystemMetrics(0) - 128)
    height = min(args.height, user32.GetSystemMetrics(1) - 128)
    if width < 400 or height < 160 or args.fps <= 0:
        raise ValueError("The display is too small, or source FPS is invalid.")

    wndproc_type = ctypes.WINFUNCTYPE(
        ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
    )

    class WindowClass(ctypes.Structure):
        _fields_ = [
            ("style", wintypes.UINT), ("lpfnWndProc", wndproc_type),
            ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
            ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
            ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
            ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR),
        ]

    user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.DefWindowProcW.restype = ctypes.c_ssize_t
    user32.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
    ]
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.GetDC.argtypes = [wintypes.HWND]
    user32.GetDC.restype = wintypes.HDC
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    user32.DestroyWindow.argtypes = [wintypes.HWND]
    user32.FillRect.argtypes = [wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.HBRUSH]
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE
    gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi32.CreateCompatibleDC.restype = wintypes.HDC
    gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
    gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
    gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    gdi32.SelectObject.restype = wintypes.HGDIOBJ
    gdi32.GetStockObject.argtypes = [ctypes.c_int]
    gdi32.GetStockObject.restype = wintypes.HGDIOBJ
    gdi32.SetDCBrushColor.argtypes = [wintypes.HDC, wintypes.COLORREF]
    gdi32.BitBlt.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                           wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.DWORD]
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    gdi32.DeleteDC.argtypes = [wintypes.HDC]

    @wndproc_type
    def window_proc(hwnd, message, wparam, lparam):
        return user32.DefWindowProcW(hwnd, message, wparam, lparam)

    instance = kernel32.GetModuleHandleW(None)
    name = "DXcamControlledCaptureBenchmark"
    window_class = WindowClass(0, window_proc, 0, 0, instance, None, None, None, None, name)
    if not user32.RegisterClassW(ctypes.byref(window_class)):
        raise ctypes.WinError()
    # TOPMOST | TOOLWINDOW | NOACTIVATE; no keyboard/mouse events are synthesized.
    hwnd = user32.CreateWindowExW(0x08000088, name, "DXcam controlled benchmark",
                                 0x90000000, 64, 64, width, height, None, None, instance, None)
    if not hwnd:
        raise ctypes.WinError()
    screen_dc = user32.GetDC(hwnd)
    memory_dc = gdi32.CreateCompatibleDC(screen_dc)
    bitmap = gdi32.CreateCompatibleBitmap(screen_dc, width, height)
    old_bitmap = gdi32.SelectObject(memory_dc, bitmap)
    brush = gdi32.GetStockObject(18)  # DC_BRUSH
    stop = threading.Event()
    threading.Thread(target=lambda: (sys.stdin.readline(), stop.set()), daemon=True).start()
    started = time.perf_counter()
    frame_id = 0
    deadline = started
    message = wintypes.MSG()

    def fill(left: int, top: int, right: int, bottom: int, color: int) -> None:
        rect = wintypes.RECT(left, top, right, bottom)
        gdi32.SetDCBrushColor(memory_dc, color)
        user32.FillRect(memory_dc, ctypes.byref(rect), brush)

    try:
        print(json.dumps({"region": [64, 64, 64 + width, 64 + height],
                          "requested_source_fps": args.fps}), flush=True)
        while not stop.is_set() and time.perf_counter() - started < args.max_seconds:
            while user32.PeekMessageW(ctypes.byref(message), None, 0, 0, 1):
                user32.TranslateMessage(ctypes.byref(message))
                user32.DispatchMessageW(ctypes.byref(message))
            frame_id += 1
            fill(0, 0, width, height, 0x302820)
            bar = (frame_id * 7) % width
            fill(bar, 80, min(bar + 100, width), height, 0xE0A050)
            bits = tuple((frame_id >> bit) & 1 for bit in range(16))
            for index, value in enumerate(MARKER + bits + tuple(1 - bit for bit in bits)):
                fill(BAR_X + index * CELL, BAR_Y, BAR_X + (index + 1) * CELL,
                     BAR_Y + CELL, 0xFFFFFF if value else 0)
            gdi32.BitBlt(screen_dc, 0, 0, width, height, memory_dc, 0, 0, 0x00CC0020)
            gdi32.GdiFlush()
            deadline += 1.0 / args.fps
            delay = deadline - time.perf_counter()
            if delay > 0:
                stop.wait(delay)
            elif delay < -0.1:
                deadline = time.perf_counter()
        elapsed = time.perf_counter() - started
        print(json.dumps({"submitted_frames": frame_id, "elapsed_s": elapsed,
                          "source_submissions_per_s": frame_id / elapsed}), flush=True)
    finally:
        gdi32.SelectObject(memory_dc, old_bitmap)
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(memory_dc)
        user32.ReleaseDC(hwnd, screen_dc)
        user32.DestroyWindow(hwnd)


if __name__ == "__main__":
    main()
