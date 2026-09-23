"""Draw a bounded, independent geometry oracle in a non-activating GDI window.

The renderer only paints its own window; it never reads desktop pixels. Coordinates
are physical pixels. Send a JSON object containing x, y, width, height and epoch on
stdin to reposition/redraw, or {"stop": true} to exit. EOF also stops the renderer.
"""

from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import json
import queue
import sys
import threading
import time


MIN_DIMENSION = 128
MAX_EPOCH = (1 << 32) - 1


def pattern_spec(width: int, height: int, epoch: int = 1) -> dict:
    """Return an asymmetric rectangle pattern and independent pixel expectations.

    Rectangles have half-open bounds and BGR byte colors, in painting order.
    Samples include near-edge points, broad patches and every epoch marker bit.
    No DXcam modules, image converters or desktop state participate in this oracle.
    """
    for name, value in (("width", width), ("height", height)):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{name} must be an integer")
        if not MIN_DIMENSION <= value <= 32768:
            raise ValueError(f"{name} must be between {MIN_DIMENSION} and 32768")
    if isinstance(epoch, bool) or not isinstance(epoch, int):
        raise ValueError("epoch must be an integer")
    if not 1 <= epoch <= MAX_EPOCH:
        raise ValueError(f"epoch must be between 1 and {MAX_EPOCH}")

    rectangles: list[dict] = []
    samples: list[dict] = []

    def add(name, rect, bgr, points=()):
        rectangles.append({"name": name, "rect": list(rect), "bgr": list(bgr)})
        for suffix, x, y in points:
            samples.append({"name": f"{name}_{suffix}", "x": x, "y": y})

    add("background", (0, 0, width, height), (32, 32, 32))
    corner_w, corner_h = width // 8, height // 8
    corners = (
        ("top_left", (0, 0, corner_w, corner_h), (0, 0, 255)),
        ("top_right", (width - corner_w, 0, width, corner_h), (0, 255, 0)),
        ("bottom_left", (0, height - corner_h, corner_w, height), (255, 0, 0)),
        (
            "bottom_right",
            (width - corner_w, height - corner_h, width, height),
            (0, 255, 255),
        ),
    )
    for name, (left, top, right, bottom), color in corners:
        add(
            name,
            (left, top, right, bottom),
            color,
            (
                ("center", (left + right) // 2, (top + bottom) // 2),
                ("near_left", left + 2, top + 2),
                ("near_right", right - 3, bottom - 3),
            ),
        )

    # Tiny pixel-font labels are part of the rectangle oracle, avoiding font
    # antialiasing or GDI text touching an expected-color sample.
    glyphs = {
        "T": ("11111", "00100", "00100", "00100", "00100", "00100", "00100"),
        "L": ("10000", "10000", "10000", "10000", "10000", "10000", "11111"),
        "B": ("11110", "10001", "10001", "11110", "10001", "10001", "11110"),
        "R": ("11110", "10001", "10001", "11110", "10100", "10010", "10001"),
    }
    scale = max(1, min(corner_w // 16, corner_h // 20))
    for (name, (left, top, _, bottom), _), label in zip(
        corners, ("TL", "TR", "BL", "BR")
    ):
        for letter_index, letter in enumerate(label):
            for row, values in enumerate(glyphs[letter]):
                for col, value in enumerate(values):
                    if value == "1":
                        glyph_x = left + 2 + (letter_index * 6 + col) * scale
                        glyph_y = bottom - 7 * scale + row * scale
                        add(
                            f"label_{name}_{letter_index}_{row}_{col}",
                            (glyph_x, glyph_y, glyph_x + scale, glyph_y + scale),
                            (255, 255, 255),
                        )

    # Different locations, dimensions and colors distinguish swapped axes and
    # mirrored or quarter-turned images even when an ROI excludes the corners.
    horizontal = (width // 6, height * 3 // 10, width * 5 // 6, height * 7 // 20)
    add(
        "horizontal_band",
        horizontal,
        (255, 255, 0),
        (
            ("left", width // 4, height * 13 // 40),
            ("right", width * 3 // 4, height * 13 // 40),
        ),
    )
    vertical = (width * 3 // 5, height // 5, width * 13 // 20, height * 4 // 5)
    add(
        "vertical_band",
        vertical,
        (255, 0, 255),
        (
            ("upper", width * 5 // 8, height // 4),
            ("lower", width * 5 // 8, height * 3 // 4),
            ("intersection", width * 5 // 8, height * 13 // 40),
        ),
    )
    add(
        "center",
        (width * 9 // 20, height * 9 // 20, width * 11 // 20, height * 11 // 20),
        (255, 255, 255),
        (("point", width // 2, height // 2),),
    )
    add(
        "offcenter_stem",
        (width // 5, height // 2, width // 4, height * 2 // 3),
        (0, 128, 255),
        (("point", width * 9 // 40, height * 7 // 12),),
    )
    add(
        "offcenter_foot",
        (width // 5, height * 3 // 5, width * 2 // 5, height * 2 // 3),
        (0, 128, 255),
        (("point", width // 3, height * 19 // 30),),
    )

    # Thirty-two bits in a deliberately off-center 8 by 4 grid. The whole epoch
    # is encoded, so updates cannot accidentally reuse an identical marker.
    cell_w, cell_h = width // 32, height // 24
    marker_x, marker_y = width // 4, height * 4 // 5
    for bit in range(32):
        left = marker_x + (bit % 8) * cell_w
        top = marker_y + (bit // 8) * cell_h
        color = (255, 255, 255) if (epoch >> bit) & 1 else (0, 0, 0)
        add(
            f"epoch_bit_{bit:02d}",
            (left, top, left + cell_w, top + cell_h),
            color,
            (("point", left + cell_w // 2, top + cell_h // 2),),
        )
        inverse_left = width * 21 // 40 + (bit % 8) * cell_w
        inverse_color = tuple(255 - value for value in color)
        add(
            f"epoch_inverse_bit_{bit:02d}",
            (inverse_left, top, inverse_left + cell_w, top + cell_h),
            inverse_color,
            (("point", inverse_left + cell_w // 2, top + cell_h // 2),),
        )

    samples.extend(
        [
            {"name": "top_edge", "x": width // 2, "y": 2},
            {"name": "bottom_edge", "x": width // 2, "y": height - 3},
            {"name": "left_edge", "x": 2, "y": height // 2},
            {"name": "right_edge", "x": width - 3, "y": height // 2},
        ]
    )
    for sample in samples:
        for rectangle in reversed(rectangles):
            left, top, right, bottom = rectangle["rect"]
            if left <= sample["x"] < right and top <= sample["y"] < bottom:
                sample["bgr"] = rectangle["bgr"].copy()
                break
    return {
        "width": width,
        "height": height,
        "epoch": epoch,
        "rectangles": rectangles,
        "samples": samples,
    }


class _Renderer:
    """Thread-affine Win32 owner; every handle API has pointer-safe signatures."""

    def __init__(self, x, y, width, height, epoch):
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.hwnd = self.dc = self.memory_dc = self.bitmap = self.old_bitmap = None
        self.instance = self.atom = self.old_dpi = None
        self.brushes = {}
        self.name = f"DXcamGeometryOracle_{time.time_ns()}"
        self._bind()
        try:
            # A fresh process normally accepts this. If its manifest or host has
            # already selected awareness, the explicit thread context still
            # guarantees physical coordinates for all window operations below.
            if not self.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
                error = ctypes.get_last_error()
                if error != 5:  # ERROR_ACCESS_DENIED: awareness already set.
                    raise ctypes.WinError(error)
            self.old_dpi = self.user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
            if not self.old_dpi:
                raise ctypes.WinError(ctypes.get_last_error())
            self.instance = self.kernel32.GetModuleHandleW(None)
            if not self.instance:
                raise ctypes.WinError(ctypes.get_last_error())

            @self.wndproc_type
            def window_proc(hwnd, message, wparam, lparam):
                return self.user32.DefWindowProcW(hwnd, message, wparam, lparam)

            self.window_proc = window_proc  # Keep callback alive through DestroyWindow.
            window_class = self.window_class_type(
                0, window_proc, 0, 0, self.instance, None, None, None, None, self.name
            )
            self.atom = self.user32.RegisterClassW(ctypes.byref(window_class))
            if not self.atom:
                raise ctypes.WinError(ctypes.get_last_error())
            self.hwnd = self.user32.CreateWindowExW(
                0x08000088,
                self.name,
                "DXcam geometry oracle",
                0x90000000,
                x,
                y,
                width,
                height,
                None,
                None,
                self.instance,
                None,
            )  # TOPMOST | TOOLWINDOW | NOACTIVATE, POPUP | VISIBLE.
            if not self.hwnd:
                raise ctypes.WinError(ctypes.get_last_error())
            self.dc = self.user32.GetDC(self.hwnd)
            if not self.dc:
                raise ctypes.WinError(ctypes.get_last_error())
            self.configure(x, y, width, height, epoch)
        except BaseException:
            self.close()
            raise

    def _bind(self):
        def bind(dll, name, result, *args):
            func = getattr(dll, name)
            func.argtypes, func.restype = list(args), result

        pointer = ctypes.c_void_p
        self.wndproc_type = ctypes.WINFUNCTYPE(
            ctypes.c_ssize_t,
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        )

        class WindowClass(ctypes.Structure):
            _fields_ = [
                ("style", wintypes.UINT),
                ("lpfnWndProc", self.wndproc_type),
                ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE),
                ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR),
            ]

        self.window_class_type = WindowClass
        bind(self.user32, "SetProcessDpiAwarenessContext", wintypes.BOOL, pointer)
        bind(self.user32, "SetThreadDpiAwarenessContext", pointer, pointer)
        bind(self.kernel32, "GetModuleHandleW", wintypes.HMODULE, wintypes.LPCWSTR)
        bind(
            self.user32,
            "DefWindowProcW",
            ctypes.c_ssize_t,
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        )
        bind(self.user32, "RegisterClassW", wintypes.ATOM, ctypes.POINTER(WindowClass))
        bind(
            self.user32,
            "UnregisterClassW",
            wintypes.BOOL,
            wintypes.LPCWSTR,
            wintypes.HINSTANCE,
        )
        bind(
            self.user32,
            "CreateWindowExW",
            wintypes.HWND,
            wintypes.DWORD,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.DWORD,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.HWND,
            wintypes.HMENU,
            wintypes.HINSTANCE,
            pointer,
        )
        bind(
            self.user32,
            "SetWindowPos",
            wintypes.BOOL,
            wintypes.HWND,
            wintypes.HWND,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.UINT,
        )
        bind(self.user32, "GetDC", wintypes.HDC, wintypes.HWND)
        bind(self.user32, "ReleaseDC", ctypes.c_int, wintypes.HWND, wintypes.HDC)
        bind(self.user32, "DestroyWindow", wintypes.BOOL, wintypes.HWND)
        bind(
            self.user32,
            "FillRect",
            ctypes.c_int,
            wintypes.HDC,
            ctypes.POINTER(wintypes.RECT),
            wintypes.HBRUSH,
        )
        bind(
            self.user32,
            "PeekMessageW",
            wintypes.BOOL,
            ctypes.POINTER(wintypes.MSG),
            wintypes.HWND,
            wintypes.UINT,
            wintypes.UINT,
            wintypes.UINT,
        )
        bind(
            self.user32, "TranslateMessage", wintypes.BOOL, ctypes.POINTER(wintypes.MSG)
        )
        bind(
            self.user32,
            "DispatchMessageW",
            ctypes.c_ssize_t,
            ctypes.POINTER(wintypes.MSG),
        )
        bind(self.gdi32, "CreateCompatibleDC", wintypes.HDC, wintypes.HDC)
        bind(
            self.gdi32,
            "CreateCompatibleBitmap",
            wintypes.HBITMAP,
            wintypes.HDC,
            ctypes.c_int,
            ctypes.c_int,
        )
        bind(self.gdi32, "CreateSolidBrush", wintypes.HBRUSH, wintypes.COLORREF)
        bind(
            self.gdi32, "SelectObject", wintypes.HGDIOBJ, wintypes.HDC, wintypes.HGDIOBJ
        )
        bind(self.gdi32, "DeleteObject", wintypes.BOOL, wintypes.HGDIOBJ)
        bind(self.gdi32, "DeleteDC", wintypes.BOOL, wintypes.HDC)
        bind(
            self.gdi32,
            "BitBlt",
            wintypes.BOOL,
            wintypes.HDC,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.HDC,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.DWORD,
        )
        bind(self.gdi32, "GdiFlush", wintypes.BOOL)

    def _discard_bitmap(self):
        if self.memory_dc:
            if self.old_bitmap:
                self.gdi32.SelectObject(self.memory_dc, self.old_bitmap)
            if self.bitmap:
                self.gdi32.DeleteObject(self.bitmap)
            self.gdi32.DeleteDC(self.memory_dc)
        self.memory_dc = self.bitmap = self.old_bitmap = None

    def configure(self, x, y, width, height, epoch):
        spec = pattern_spec(width, height, epoch)
        for name, value in (("x", x), ("y", y)):
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not -(1 << 30) < value < (1 << 30)
            ):
                raise ValueError(f"{name} must be a signed pixel coordinate")
        if not self.user32.SetWindowPos(
            self.hwnd, wintypes.HWND(-1), x, y, width, height, 0x0050
        ):
            raise ctypes.WinError(ctypes.get_last_error())  # NOACTIVATE | SHOWWINDOW.
        self._discard_bitmap()
        self.memory_dc = self.gdi32.CreateCompatibleDC(self.dc)
        if not self.memory_dc:
            raise ctypes.WinError(ctypes.get_last_error())
        self.bitmap = self.gdi32.CreateCompatibleBitmap(self.dc, width, height)
        if not self.bitmap:
            raise ctypes.WinError(ctypes.get_last_error())
        self.old_bitmap = self.gdi32.SelectObject(self.memory_dc, self.bitmap)
        if not self.old_bitmap or self.old_bitmap == ctypes.c_void_p(-1).value:
            self.old_bitmap = None
            raise ctypes.WinError(ctypes.get_last_error())
        self.x, self.y, self.width, self.height, self.epoch = x, y, width, height, epoch
        self.spec = spec
        self.draw()

    def draw(self):
        for rectangle in self.spec["rectangles"]:
            bgr = tuple(rectangle["bgr"])
            brush = self.brushes.get(bgr)
            if not brush:
                blue, green, red = bgr
                brush = self.gdi32.CreateSolidBrush(red | green << 8 | blue << 16)
                if not brush:
                    raise ctypes.WinError(ctypes.get_last_error())
                self.brushes[bgr] = brush
            rect = wintypes.RECT(*rectangle["rect"])
            if not self.user32.FillRect(self.memory_dc, ctypes.byref(rect), brush):
                raise ctypes.WinError(ctypes.get_last_error())
        if not self.gdi32.BitBlt(
            self.dc, 0, 0, self.width, self.height, self.memory_dc, 0, 0, 0x00CC0020
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        if not self.gdi32.GdiFlush():
            raise ctypes.WinError(ctypes.get_last_error())
        self.painted_at_perf_counter_ns = time.perf_counter_ns()

    def acknowledge(self, event):
        print(
            json.dumps(
                {
                    "event": event,
                    "x": self.x,
                    "y": self.y,
                    "width": self.width,
                    "height": self.height,
                    "epoch": self.epoch,
                    "painted_at_perf_counter_ns": self.painted_at_perf_counter_ns,
                    "region": [
                        self.x,
                        self.y,
                        self.x + self.width,
                        self.y + self.height,
                    ],
                }
            ),
            flush=True,
        )

    def pump(self):
        message = wintypes.MSG()
        while self.user32.PeekMessageW(ctypes.byref(message), None, 0, 0, 1):
            if message.message == 0x0012:  # WM_QUIT
                return False
            self.user32.TranslateMessage(ctypes.byref(message))
            self.user32.DispatchMessageW(ctypes.byref(message))
        return True

    def close(self):
        self._discard_bitmap()
        for brush in self.brushes.values():
            self.gdi32.DeleteObject(brush)
        self.brushes.clear()
        if self.dc:
            self.user32.ReleaseDC(self.hwnd, self.dc)
            self.dc = None
        if self.hwnd:
            self.user32.DestroyWindow(self.hwnd)
            self.hwnd = None
        if self.atom:
            self.user32.UnregisterClassW(self.name, self.instance)
            self.atom = None
        if self.old_dpi:
            self.user32.SetThreadDpiAwarenessContext(self.old_dpi)
            self.old_dpi = None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--x", type=int, default=0)
    parser.add_argument("--y", type=int, default=0)
    parser.add_argument("--width", type=int, required=True)
    parser.add_argument("--height", type=int, required=True)
    parser.add_argument("--epoch", type=int, default=1)
    parser.add_argument("--max-seconds", type=float, default=120)
    args = parser.parse_args()
    if not 0 < args.max_seconds <= 180:
        parser.error("--max-seconds must be greater than zero and at most 180")
    pattern_spec(args.width, args.height, args.epoch)
    if sys.platform != "win32":
        parser.error("The geometry renderer requires Windows")
    commands = queue.Queue(maxsize=16)

    def read_commands():
        for line in sys.stdin:
            try:
                command = json.loads(line)
            except (ValueError, TypeError) as error:
                commands.put({"error": str(error)})
            else:
                commands.put(command)
        commands.put({"stop": True})

    threading.Thread(target=read_commands, daemon=True).start()
    renderer = _Renderer(args.x, args.y, args.width, args.height, args.epoch)
    started = time.monotonic()
    try:
        renderer.acknowledge("ready")
        next_draw = started + 0.1
        while time.monotonic() - started < args.max_seconds and renderer.pump():
            try:
                command = commands.get(
                    timeout=max(0, min(0.02, next_draw - time.monotonic()))
                )
            except queue.Empty:
                command = None
            if command is not None:
                if not isinstance(command, dict):
                    raise ValueError("Each command must be a JSON object")
                if command.get("stop") is True:
                    break
                if "error" in command:
                    raise ValueError(command["error"])
                renderer.configure(
                    *(command[name] for name in ("x", "y", "width", "height", "epoch"))
                )
                renderer.acknowledge("updated")
            if time.monotonic() >= next_draw:
                renderer.draw()
                next_draw = time.monotonic() + 0.1
    finally:
        renderer.close()
    print(json.dumps({"event": "stopped"}), flush=True)


if __name__ == "__main__":
    main()
