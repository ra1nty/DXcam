from __future__ import annotations

import ctypes
import importlib.util
import io
import json
import queue
from pathlib import Path
from types import SimpleNamespace

import pytest


_SPEC = importlib.util.spec_from_file_location(
    "geometry_display", Path(__file__).parents[1] / "benchmarks" / "geometry_display.py"
)
assert _SPEC is not None and _SPEC.loader is not None
display = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(display)


def mode(name="DISPLAY1", **changes):
    return {
        "device_name": name,
        "width": 1920,
        "height": 1080,
        "x": 0,
        "y": 0,
        "orientation": 0,
        "frequency": 60,
        "bpp": 32,
        "fields": 0,
        **changes,
    }


class FakeDisplays:
    def __init__(self):
        self.modes = {"DISPLAY1": mode(), "DISPLAY2": mode("DISPLAY2", x=1920)}
        self.calls = []
        self.apply_code = 0

    def EnumDisplaySettingsW(self, name, index, address):
        if name not in self.modes or index not in (0, display.ENUM_CURRENT_SETTINGS):
            return 0
        native = display._native_mode(name, self.modes[name])
        ctypes.memmove(address, ctypes.byref(native), ctypes.sizeof(native))
        return 1

    def EnumDisplayDevicesW(self, parent, index, address, flags):
        if index >= 3:
            return 0
        result = ctypes.cast(address, ctypes.POINTER(display.DISPLAY_DEVICEW)).contents
        assert result.cb == ctypes.sizeof(display.DISPLAY_DEVICEW)
        result.DeviceName = f"DISPLAY{index + 1}"
        result.DeviceString = "Fake display"
        result.StateFlags = (5, 1, 0)[index]
        return 1

    def ChangeDisplaySettingsExW(self, name, address, window, flags, argument):
        native = ctypes.cast(address, ctypes.POINTER(display.DEVMODEW)).contents
        result = display._mode_dict(name, native)
        self.calls.append((name, flags, result))
        assert window is None and argument is None
        if not flags and self.apply_code == 0:
            self.modes[name] = result
        return self.apply_code


@pytest.fixture
def api(monkeypatch):
    fake = FakeDisplays()
    monkeypatch.setattr(display, "_user32", fake)
    return fake


def test_inventory_omits_detached_modes_and_preserves_primary(api):
    inventory = display.list_displays()
    assert [item["attached"] for item in inventory] == [True, True, False]
    assert [item["primary"] for item in inventory] == [True, False, False]
    assert inventory[2]["current_mode"] is None
    snapshot = display.snapshot_displays()
    assert len(snapshot) == 2
    assert snapshot[0]["primary"]
    assert snapshot[1]["x"] == 1920
    assert len(display.enumerate_modes("DISPLAY1")) == 1


def test_current_mode_failure_is_not_silently_empty(api):
    with pytest.raises(RuntimeError, match="Cannot read current mode"):
        display.current_mode("missing")


@pytest.mark.parametrize("start", (0, 1, 2, 3))
@pytest.mark.parametrize("degrees", (0, 90, 180, 270))
def test_rotation_swaps_relative_to_current_orientation(start, degrees):
    original = mode(orientation=start, width=987, height=654)
    result = display.rotation_mode(original, degrees)
    expected = (654, 987) if (degrees // 90 - start) % 2 else (987, 654)
    assert (result["width"], result["height"]) == expected
    assert result["orientation"] == degrees // 90
    assert original["orientation"] == start


@pytest.mark.parametrize("degrees", (True, 45, -90, 360, "90", 90.0))
def test_invalid_rotation_is_rejected(degrees):
    with pytest.raises(ValueError, match="Rotation must"):
        display.rotation_mode(mode(), degrees)


def test_test_mode_does_not_apply_and_apply_never_saves_registry(api):
    requested = display.rotation_mode(display.current_mode("DISPLAY1"), 90)
    assert display.test_mode("DISPLAY1", requested) == 0
    assert display.current_mode("DISPLAY1")["orientation"] == 0
    assert display.apply_mode("DISPLAY1", requested) == 0
    assert display.current_mode("DISPLAY1")["orientation"] == 1
    assert [call[1] for call in api.calls] == [display.CDS_TEST, 0]
    assert api.calls[-1][2]["fields"] & display.DM_POSITION
    assert api.calls[-1][2]["fields"] & display.DM_DISPLAYORIENTATION


@pytest.mark.parametrize(
    "changes", ({"device_name": "wrong"}, {"width": 0}, {"orientation": 4})
)
def test_invalid_native_modes_are_rejected_before_api_call(api, changes):
    with pytest.raises(ValueError):
        display.apply_mode("DISPLAY1", mode(**changes))
    assert api.calls == []


def test_restore_verifies_all_outputs_and_desktop_positions(api):
    snapshot = display.snapshot_displays()
    api.modes["DISPLAY1"] = mode(width=1080, height=1920, orientation=1)
    api.modes["DISPLAY2"] = mode("DISPLAY2", x=1080, y=-200, frequency=120)
    result = display.restore_snapshot(snapshot)
    assert result["restored"]
    assert len(result["calls"]) == 2
    assert [(call[0], call[1]) for call in api.calls] == [
        ("DISPLAY1", 0),
        ("DISPLAY2", 0),
    ]
    assert api.modes["DISPLAY2"]["x"] == 1920
    assert api.modes["DISPLAY2"]["y"] == 0
    assert api.modes["DISPLAY2"]["frequency"] == 60


def test_restore_retries_and_does_not_confuse_return_code_with_geometry(api):
    snapshot = display.snapshot_displays()
    api.modes["DISPLAY1"]["width"] = 1000
    api.apply_code = -2
    result = display.restore_snapshot(snapshot)
    assert not result["restored"]
    assert len(result["calls"]) == 6
    assert result["mismatches"][0]["differences"]["width"]["actual"] == 1000


def test_unchanged_restore_does_not_reapply_modes(api):
    assert display.restore_snapshot(display.snapshot_displays())["restored"]
    assert api.calls == []


@pytest.mark.parametrize(
    ("command", "alive", "timeout", "reason"),
    [
        ("EOF", True, 1, "pipe_eof"),
        ("oops", True, 1, "invalid_command"),
        (None, False, 1, "parent_exit"),
        (None, True, 0, "deadline"),
    ],
)
def test_watchdog_failure_paths_restore(api, command, alive, timeout, reason):
    snapshot = display.snapshot_displays()
    api.modes["DISPLAY1"]["orientation"] = 2
    messages = queue.Queue()
    if command is not None:
        messages.put(command)
    result = display._watchdog_loop(snapshot, messages, lambda: alive, timeout)
    assert result["reason"] == reason
    assert result["restored"]
    assert api.modes["DISPLAY1"]["orientation"] == 0


def test_watchdog_disarm_independently_verifies_geometry(api):
    snapshot = display.snapshot_displays()
    api.modes["DISPLAY1"]["orientation"] = 2
    messages = queue.Queue()
    messages.put("DISARM")
    result = display._watchdog_loop(snapshot, messages, lambda: True, 1)
    assert result["reason"] == "disarm_verification_failed"
    assert result["restored"]
    assert api.calls


def test_watchdog_accepts_disarm_only_when_already_restored(api):
    messages = queue.Queue()
    messages.put("DISARM")
    result = display._watchdog_loop(
        display.snapshot_displays(), messages, lambda: True, 1
    )
    assert result["reason"] == "disarmed"
    assert result["restored"]
    assert api.calls == []


def test_pipe_reader_treats_eof_as_restore_signal():
    messages = queue.Queue()
    display._read_command(io.StringIO(), messages)
    assert messages.get_nowait() == "EOF"


def test_guard_rejects_empty_snapshot_or_unbounded_deadline(tmp_path):
    with pytest.raises(ValueError):
        display.RestoreGuard([], tmp_path)
    for timeout in (0, 601, float("inf"), float("nan")):
        with pytest.raises(ValueError):
            display.RestoreGuard([mode()], tmp_path, timeout_s=timeout)


def test_guard_check_requires_living_watchdog_and_deadline(monkeypatch, tmp_path):
    guard = display.RestoreGuard([mode()], tmp_path)
    with pytest.raises(RuntimeError, match="not armed"):
        guard.check()
    guard.process = SimpleNamespace(poll=lambda: None)
    guard._entered = True
    guard._deadline = 100
    monkeypatch.setattr(display.time, "monotonic", lambda: 90)
    guard.check()
    monkeypatch.setattr(display.time, "monotonic", lambda: 99)
    with pytest.raises(TimeoutError, match="deadline"):
        guard.check()


class RecordingInput(io.StringIO):
    saved = ""

    def close(self):
        self.saved = self.getvalue()
        super().close()


class FakeWatchdog:
    def __init__(self, arguments, *, ready="READY\n", **kwargs):
        self.stdin = RecordingInput()
        self.stdout = io.StringIO(ready)
        self.returncode = None
        self.status_path = Path(arguments[arguments.index("--status") + 1])
        self.arguments = arguments
        self.kwargs = kwargs

    def poll(self):
        return self.returncode

    def wait(self, timeout):
        self.returncode = 0
        self.status_path.write_text(
            json.dumps({"reason": "disarmed", "restored": True}), encoding="utf-8"
        )
        return 0


def test_guard_restores_on_exception_then_disarms_hidden_child(
    api, monkeypatch, tmp_path
):
    children = []

    def launch(arguments, **kwargs):
        child = FakeWatchdog(arguments, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(display.subprocess, "Popen", launch)
    snapshot = display.snapshot_displays()
    guard = display.RestoreGuard(snapshot, tmp_path)
    with pytest.raises(ValueError, match="capture failed"):
        with guard:
            api.modes["DISPLAY2"]["x"] = -1000
            raise ValueError("capture failed")
    assert guard.status["restored"]
    assert guard.watchdog_status["restored"]
    assert children[0].stdin.saved == "DISARM\n"
    assert children[0].kwargs["creationflags"] == display.subprocess.CREATE_NO_WINDOW
    assert children[0].kwargs["close_fds"]
    assert api.modes["DISPLAY2"]["x"] == snapshot[1]["x"]
    assert json.loads(guard.snapshot_path.read_text()) == snapshot


def test_guard_will_not_disarm_if_parent_restore_cannot_verify(
    api, monkeypatch, tmp_path
):
    children = []

    def launch(arguments, **kwargs):
        child = FakeWatchdog(arguments, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(display.subprocess, "Popen", launch)
    with pytest.raises(RuntimeError, match="restoration could not verify"):
        with display.RestoreGuard(display.snapshot_displays(), tmp_path):
            api.modes["DISPLAY1"]["width"] = 1000
            api.apply_code = -2
    assert children[0].stdin.saved == ""
    assert children[0].stdin.closed


def test_guard_requires_ready_handshake_before_context_entry(
    api, monkeypatch, tmp_path
):
    children = []

    def launch(arguments, **kwargs):
        child = FakeWatchdog(arguments, ready="", **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(display.subprocess, "Popen", launch)
    entered = False
    with pytest.raises(RuntimeError, match="did not become ready"):
        with display.RestoreGuard(display.snapshot_displays(), tmp_path):
            entered = True
    assert not entered
    assert children[0].stdin.closed
    assert api.calls == []
