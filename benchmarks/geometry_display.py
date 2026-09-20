"""Temporary Windows display modes and an independent restoration watchdog.

``snapshot_displays()`` captures every attached output. Enter ``RestoreGuard``
*before* any mode change, call ``guard.check()`` immediately before each change,
and leave the context after the capture experiment. The guard restores and
verifies all saved dimensions, orientations, positions, refresh rates and bpp.
Its hidden subprocess restores on parent exit, pipe EOF, or its fixed deadline.
Only a verified restoration permits disarming the watchdog.

Mode dictionaries are JSON serializable. ``orientation`` is DEVMODE's 0..3;
``rotation_mode(mode, degrees)`` takes an absolute 0/90/180/270 orientation
(counter-clockwise from the display's natural orientation). Changes use flags
zero, never CDS_UPDATEREGISTRY. Multi-output restoration is sequential, retried,
and verified: Windows' documented atomic alternative would modify the registry.
Driver calls themselves cannot be given a Python timeout; a failing driver or
disconnected monitor is reported explicitly, rather than called restored.

Primary references:
https://learn.microsoft.com/windows/win32/api/winuser/nf-winuser-changedisplaysettingsexw
https://learn.microsoft.com/windows/win32/api/winuser/nf-winuser-enumdisplaydevicesw
https://learn.microsoft.com/windows/win32/api/wingdi/ns-wingdi-devmodew
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import json
import math
import os
import queue
import subprocess
import sys
import threading
import time
import uuid
from ctypes import wintypes
from pathlib import Path
from typing import Any

ENUM_CURRENT_SETTINGS = 0xFFFFFFFF
CDS_TEST = 0x00000002
DISP_CHANGE_SUCCESSFUL = 0
DM_POSITION = 0x00000020
DM_DISPLAYORIENTATION = 0x00000080
DM_BITSPERPEL = 0x00040000
DM_PELSWIDTH = 0x00080000
DM_PELSHEIGHT = 0x00100000
DM_DISPLAYFLAGS = 0x00200000
DM_DISPLAYFREQUENCY = 0x00400000
DISPLAY_DEVICE_ATTACHED_TO_DESKTOP = 0x00000001
DISPLAY_DEVICE_PRIMARY_DEVICE = 0x00000004
_MODE_FIELDS = (
    DM_POSITION
    | DM_DISPLAYORIENTATION
    | DM_BITSPERPEL
    | DM_PELSWIDTH
    | DM_PELSHEIGHT
    | DM_DISPLAYFLAGS
    | DM_DISPLAYFREQUENCY
)
_VERIFY_KEYS = ("width", "height", "x", "y", "orientation", "frequency", "bpp")
_user32: Any = None


class DEVMODEW(ctypes.Structure):
    # The display arm of the first union occupies the same 16 bytes as its
    # printer arm. The second union is represented by dmDisplayFlags.
    _fields_ = [
        ("dmDeviceName", wintypes.WCHAR * 32),
        ("dmSpecVersion", wintypes.WORD),
        ("dmDriverVersion", wintypes.WORD),
        ("dmSize", wintypes.WORD),
        ("dmDriverExtra", wintypes.WORD),
        ("dmFields", wintypes.DWORD),
        ("dmPositionX", wintypes.LONG),
        ("dmPositionY", wintypes.LONG),
        ("dmDisplayOrientation", wintypes.DWORD),
        ("dmDisplayFixedOutput", wintypes.DWORD),
        ("dmColor", wintypes.SHORT),
        ("dmDuplex", wintypes.SHORT),
        ("dmYResolution", wintypes.SHORT),
        ("dmTTOption", wintypes.SHORT),
        ("dmCollate", wintypes.SHORT),
        ("dmFormName", wintypes.WCHAR * 32),
        ("dmLogPixels", wintypes.WORD),
        ("dmBitsPerPel", wintypes.DWORD),
        ("dmPelsWidth", wintypes.DWORD),
        ("dmPelsHeight", wintypes.DWORD),
        ("dmDisplayFlags", wintypes.DWORD),
        ("dmDisplayFrequency", wintypes.DWORD),
        ("dmICMMethod", wintypes.DWORD),
        ("dmICMIntent", wintypes.DWORD),
        ("dmMediaType", wintypes.DWORD),
        ("dmDitherType", wintypes.DWORD),
        ("dmReserved1", wintypes.DWORD),
        ("dmReserved2", wintypes.DWORD),
        ("dmPanningWidth", wintypes.DWORD),
        ("dmPanningHeight", wintypes.DWORD),
    ]


class DISPLAY_DEVICEW(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("DeviceName", wintypes.WCHAR * 32),
        ("DeviceString", wintypes.WCHAR * 128),
        ("StateFlags", wintypes.DWORD),
        ("DeviceID", wintypes.WCHAR * 128),
        ("DeviceKey", wintypes.WCHAR * 128),
    ]


def _api() -> Any:
    global _user32
    if _user32 is None:
        if sys.platform != "win32":
            raise RuntimeError("Display mode validation requires Windows.")
        api = ctypes.WinDLL("user32", use_last_error=True)
        api.EnumDisplaySettingsW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            ctypes.POINTER(DEVMODEW),
        ]
        api.EnumDisplaySettingsW.restype = wintypes.BOOL
        api.EnumDisplayDevicesW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            ctypes.POINTER(DISPLAY_DEVICEW),
            wintypes.DWORD,
        ]
        api.EnumDisplayDevicesW.restype = wintypes.BOOL
        api.ChangeDisplaySettingsExW.argtypes = [
            wintypes.LPCWSTR,
            ctypes.POINTER(DEVMODEW),
            wintypes.HWND,
            wintypes.DWORD,
            ctypes.c_void_p,
        ]
        api.ChangeDisplaySettingsExW.restype = wintypes.LONG
        _user32 = api
    return _user32


def _new_mode() -> DEVMODEW:
    mode = DEVMODEW()
    mode.dmSize = ctypes.sizeof(mode)
    return mode


def _mode_dict(device_name: str, mode: DEVMODEW) -> dict[str, Any]:
    return {
        "device_name": device_name,
        "width": int(mode.dmPelsWidth),
        "height": int(mode.dmPelsHeight),
        "x": int(mode.dmPositionX),
        "y": int(mode.dmPositionY),
        "orientation": int(mode.dmDisplayOrientation),
        "frequency": int(mode.dmDisplayFrequency),
        "bpp": int(mode.dmBitsPerPel),
        "fields": int(mode.dmFields),
        "display_flags": int(mode.dmDisplayFlags),
        "devmode_base64": base64.b64encode(bytes(mode)).decode("ascii"),
    }


def _native_mode(device_name: str, mode: dict[str, Any]) -> DEVMODEW:
    if mode["device_name"] != device_name:
        raise ValueError("Mode device does not match the requested display.")
    if mode["width"] <= 0 or mode["height"] <= 0:
        raise ValueError("The geometry harness cannot detach displays.")
    if mode["orientation"] not in (0, 1, 2, 3):
        raise ValueError("Invalid DEVMODE orientation.")
    if "devmode_base64" in mode:
        raw = base64.b64decode(mode["devmode_base64"], validate=True)
        if len(raw) != ctypes.sizeof(DEVMODEW):
            raise ValueError("Saved DEVMODE has an incompatible size.")
        native = DEVMODEW.from_buffer_copy(raw)
    else:
        native = _new_mode()
    native.dmSize = ctypes.sizeof(native)
    native.dmDriverExtra = 0
    native.dmPelsWidth = mode["width"]
    native.dmPelsHeight = mode["height"]
    native.dmPositionX = mode["x"]
    native.dmPositionY = mode["y"]
    native.dmDisplayOrientation = mode["orientation"]
    native.dmDisplayFrequency = mode["frequency"]
    native.dmBitsPerPel = mode["bpp"]
    native.dmDisplayFlags = mode.get("display_flags", 0)
    # Deliberately limit changes to the display fields this harness manages.
    native.dmFields = _MODE_FIELDS
    return native


def current_mode(device_name: str) -> dict[str, Any]:
    mode = _new_mode()
    if not _api().EnumDisplaySettingsW(
        device_name, ENUM_CURRENT_SETTINGS, ctypes.byref(mode)
    ):
        raise RuntimeError(f"Cannot read current mode for {device_name}.")
    return _mode_dict(device_name, mode)


def enumerate_modes(device_name: str) -> list[dict[str, Any]]:
    modes = []
    index = 0
    while True:
        mode = _new_mode()
        if not _api().EnumDisplaySettingsW(device_name, index, ctypes.byref(mode)):
            return modes
        modes.append(_mode_dict(device_name, mode))
        index += 1


def list_displays() -> list[dict[str, Any]]:
    """List display devices; detached devices have ``current_mode=None``."""
    displays = []
    index = 0
    while True:
        device = DISPLAY_DEVICEW()
        device.cb = ctypes.sizeof(device)
        if not _api().EnumDisplayDevicesW(None, index, ctypes.byref(device), 0):
            return displays
        attached = bool(device.StateFlags & DISPLAY_DEVICE_ATTACHED_TO_DESKTOP)
        displays.append(
            {
                "name": device.DeviceName,
                "description": device.DeviceString,
                "attached": attached,
                "primary": bool(device.StateFlags & DISPLAY_DEVICE_PRIMARY_DEVICE),
                "state_flags": int(device.StateFlags),
                "current_mode": current_mode(device.DeviceName) if attached else None,
            }
        )
        index += 1


def snapshot_displays() -> list[dict[str, Any]]:
    """Capture all attached modes, with primary metadata, before any mutation."""
    return [
        {**display["current_mode"], "primary": display["primary"]}
        for display in list_displays()
        if display["attached"]
    ]


def rotation_mode(original: dict[str, Any], degrees: int) -> dict[str, Any]:
    if (
        isinstance(degrees, bool)
        or not isinstance(degrees, int)
        or degrees not in (0, 90, 180, 270)
    ):
        raise ValueError("Rotation must be 0, 90, 180, or 270 degrees.")
    result = dict(original)
    orientation = degrees // 90
    if (orientation - original["orientation"]) % 2:
        result["width"], result["height"] = original["height"], original["width"]
    result["orientation"] = orientation
    result["fields"] = _MODE_FIELDS
    return result


def test_mode(device_name: str, mode: dict[str, Any]) -> int:
    """Return CDS_TEST's result without applying or persisting the mode."""
    native = _native_mode(device_name, mode)
    return int(
        _api().ChangeDisplaySettingsExW(
            device_name, ctypes.byref(native), None, CDS_TEST, None
        )
    )


def apply_mode(device_name: str, mode: dict[str, Any]) -> int:
    """Apply a dynamic mode (flags=0); the caller must first arm RestoreGuard."""
    native = _native_mode(device_name, mode)
    return int(
        _api().ChangeDisplaySettingsExW(
            device_name, ctypes.byref(native), None, 0, None
        )
    )


def verify_snapshot(snapshot: list[dict[str, Any]]) -> dict[str, Any]:
    mismatches = []
    observed = []
    for saved in snapshot:
        name = saved["device_name"]
        try:
            actual = current_mode(name)
            observed.append(actual)
            differing = {
                key: {"expected": saved[key], "actual": actual[key]}
                for key in _VERIFY_KEYS
                if saved[key] != actual[key]
            }
            if differing:
                mismatches.append({"device_name": name, "differences": differing})
        except Exception as error:
            mismatches.append({"device_name": name, "error": repr(error)})
    return {"restored": not mismatches, "mismatches": mismatches, "observed": observed}


def restore_snapshot(
    snapshot: list[dict[str, Any]], *, attempts: int = 3
) -> dict[str, Any]:
    """Restore every saved output, retrying topology adjustments, then verify."""
    calls: list[dict[str, Any]] = []
    result = verify_snapshot(snapshot)
    for attempt in range(attempts):
        if result["restored"]:
            break
        # Returning the primary to its original size first usually permits the
        # neighboring outputs to return to their saved desktop coordinates.
        for saved in sorted(snapshot, key=lambda item: not item.get("primary", False)):
            name = saved["device_name"]
            try:
                code = apply_mode(name, saved)
                calls.append(
                    {"attempt": attempt + 1, "device_name": name, "code": code}
                )
            except Exception as error:
                calls.append(
                    {"attempt": attempt + 1, "device_name": name, "error": repr(error)}
                )
        result = verify_snapshot(snapshot)
    return {**result, "calls": calls}


def _write_json(path: Path, value: Any) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _read_command(stream: Any, messages: queue.Queue[str]) -> None:
    try:
        line = stream.readline()
        messages.put(line.strip() if line else "EOF")
    except Exception:
        messages.put("EOF")


class _ParentProcess:
    def __init__(self, pid: int):
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        self.api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.api.OpenProcess.restype = wintypes.HANDLE
        self.api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.api.WaitForSingleObject.restype = wintypes.DWORD
        self.api.CloseHandle.argtypes = [wintypes.HANDLE]
        self.api.CloseHandle.restype = wintypes.BOOL
        self.handle = self.api.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())

    def alive(self) -> bool:
        result = self.api.WaitForSingleObject(self.handle, 0)
        if result == 0xFFFFFFFF:
            raise ctypes.WinError(ctypes.get_last_error())
        return result == 0x00000102  # WAIT_TIMEOUT

    def close(self) -> None:
        self.api.CloseHandle(self.handle)


def _watchdog_loop(
    snapshot: list[dict[str, Any]],
    messages: queue.Queue[str],
    parent_alive: Any,
    timeout_s: float,
) -> dict[str, Any]:
    """Pure protocol boundary: any exit except verified DISARM restores."""
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            if not parent_alive():
                reason = "parent_exit"
                break
        except Exception:
            reason = "parent_check_failed"
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            reason = "deadline"
            break
        try:
            command = messages.get(timeout=min(0.05, remaining))
        except queue.Empty:
            continue
        if command == "DISARM":
            verified = verify_snapshot(snapshot)
            if verified["restored"]:
                return {"reason": "disarmed", **verified}
            reason = "disarm_verification_failed"
        else:
            reason = "pipe_eof" if command == "EOF" else "invalid_command"
        break
    return {"reason": reason, **restore_snapshot(snapshot)}


def _watchdog(
    snapshot_path: Path, status_path: Path, pid: int, timeout_s: float
) -> int:
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    parent = None
    try:
        # Bind native functions and open the parent handle before READY. The
        # caller cannot mutate modes unless this handshake succeeds.
        _api()
        parent = _ParentProcess(pid)
        messages: queue.Queue[str] = queue.Queue()
        threading.Thread(
            target=_read_command, args=(sys.stdin, messages), daemon=True
        ).start()
        print("READY", flush=True)
        result = _watchdog_loop(snapshot, messages, parent.alive, timeout_s)
    except BaseException as error:
        result = {
            "reason": "watchdog_error",
            "error": repr(error),
            **restore_snapshot(snapshot),
        }
    finally:
        if parent is not None:
            parent.close()
    _write_json(status_path, result)
    return 0 if result["restored"] else 1


class RestoreGuard:
    """Arm a hidden independent process, restore on exit, verify before disarm.

    ``snapshot`` is the result of ``snapshot_displays()``; ``directory`` holds
    a uniquely named subdirectory with the original snapshot and both parent
    and watchdog reports. ``check()`` refuses mutations if the deadline is too
    close or the watchdog has exited. ``restore()`` can be called between cases
    without disarming. Context exit raises if final restoration cannot verify.
    """

    def __init__(
        self,
        snapshot: list[dict[str, Any]],
        directory: str | Path,
        timeout_s: float = 120.0,
    ):
        if not snapshot:
            raise ValueError(
                "A restoration guard requires at least one attached output."
            )
        if not math.isfinite(timeout_s) or not 5 <= timeout_s <= 600:
            raise ValueError("Watchdog timeout must be between 5 and 600 seconds.")
        self.snapshot = json.loads(json.dumps(snapshot))
        self.directory = Path(directory).resolve() / f"restore_guard_{uuid.uuid4().hex}"
        self.snapshot_path = self.directory / "original_modes.json"
        self.status_path = self.directory / "parent_restore.json"
        self.watchdog_status_path = self.directory / "watchdog_restore.json"
        self.timeout_s = timeout_s
        self.status: dict[str, Any] | None = None
        self.watchdog_status: dict[str, Any] | None = None
        self.process: subprocess.Popen[str] | None = None
        self._deadline = 0.0
        self._entered = False

    def __enter__(self) -> RestoreGuard:
        if self.process is not None:
            raise RuntimeError("RestoreGuard cannot be entered twice.")
        self.directory.mkdir(parents=True, exist_ok=False)
        _write_json(self.snapshot_path, self.snapshot)
        self._deadline = time.monotonic() + self.timeout_s
        with (self.directory / "watchdog_stderr.txt").open(
            "w", encoding="utf-8"
        ) as log:
            self.process = subprocess.Popen(
                [
                    sys.executable,
                    "-I",
                    str(Path(__file__).resolve()),
                    "--watchdog",
                    str(self.snapshot_path),
                    "--status",
                    str(self.watchdog_status_path),
                    "--parent-pid",
                    str(os.getpid()),
                    "--timeout",
                    str(self.timeout_s),
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=log,
                text=True,
                creationflags=subprocess.CREATE_NO_WINDOW,
                close_fds=True,
            )
        messages: queue.Queue[str] = queue.Queue()
        threading.Thread(
            target=_read_command, args=(self.process.stdout, messages), daemon=True
        ).start()
        try:
            ready = messages.get(timeout=min(10.0, self.timeout_s - 2))
            if ready != "READY" or self.process.poll() is not None:
                raise RuntimeError(
                    f"Restoration watchdog did not become ready: {ready}"
                )
            self._entered = True
            self.check()
            return self
        except BaseException:
            # EOF tells even a late-starting child to restore and exit. Never
            # terminate a watchdog that might already own restoration.
            self._close_input()
            raise

    def check(self) -> None:
        if not self._entered or self.process is None or self.process.poll() is not None:
            raise RuntimeError("The restoration watchdog is not armed.")
        if time.monotonic() >= self._deadline - 2:
            raise TimeoutError("Display experiment reached its restoration deadline.")

    def restore(self) -> dict[str, Any]:
        self.status = restore_snapshot(self.snapshot)
        _write_json(self.status_path, self.status)
        return self.status

    def _close_input(self) -> None:
        if self.process is not None and self.process.stdin is not None:
            try:
                self.process.stdin.close()
            except OSError:
                pass

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
        try:
            result = self.restore()
            if not result["restored"]:
                raise RuntimeError(
                    f"Display restoration could not verify: {self.status_path}"
                )
            if self.process is not None and self.process.poll() is None:
                assert self.process.stdin is not None
                try:
                    self.process.stdin.write("DISARM\n")
                    self.process.stdin.flush()
                except (BrokenPipeError, OSError):
                    # The child may have restored on its deadline concurrently.
                    pass
        finally:
            self._entered = False
            self._close_input()
            if self.process is not None:
                try:
                    self.process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    # Keep the independent guard alive to finish restoration.
                    if exc is None:
                        raise RuntimeError("Restoration watchdog has not completed.")
                if self.process.poll() is not None and self.process.stdout is not None:
                    self.process.stdout.close()
        if self.watchdog_status_path.exists():
            self.watchdog_status = json.loads(
                self.watchdog_status_path.read_text(encoding="utf-8")
            )
        if not self.watchdog_status or not self.watchdog_status.get("restored"):
            raise RuntimeError(
                f"Restoration watchdog did not verify success: {self.watchdog_status_path}"
            )
        final_state = verify_snapshot(self.snapshot)
        if not final_state["restored"]:
            raise RuntimeError("Display geometry changed during watchdog shutdown.")
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--watchdog", type=Path)
    parser.add_argument("--status", type=Path)
    parser.add_argument("--parent-pid", type=int)
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args()
    if args.watchdog:
        if args.status is None or args.parent_pid is None:
            parser.error("--watchdog requires --status and --parent-pid")
        return _watchdog(args.watchdog, args.status, args.parent_pid, args.timeout)
    print(json.dumps(list_displays(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
