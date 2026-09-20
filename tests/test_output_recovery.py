from __future__ import annotations

import ctypes
from threading import RLock
from types import SimpleNamespace

import comtypes
import pytest

from dxcam._libs.dxgi import DXGI_ERROR_ACCESS_LOST, DXGI_OUTPUT_DESC
from dxcam.core.output import Output
from dxcam.runtime.output_recovery import OutputRecoveryHandler


def access_lost() -> comtypes.COMError:
    return comtypes.COMError(DXGI_ERROR_ACCESS_LOST, "Controlled access loss", None)


class FakeOutputPointer:
    def __init__(self, name="NEW_OUTPUT", monitor=55, errors=None):
        self.desc = DXGI_OUTPUT_DESC()
        self.desc.DeviceName = name
        self.desc.Monitor = monitor
        self.desc.AttachedToDesktop = True
        self.desc.DesktopCoordinates.right = 1920
        self.desc.DesktopCoordinates.bottom = 1080
        self.desc.Rotation = 1
        self.errors = errors or {}
        self.get_desc_calls = 0
        self.release_calls = 0

    def GetDesc(self, destination):
        assert self.release_calls == 0, "GetDesc used a released COM pointer"
        self.get_desc_calls += 1
        error = self.errors.get(self.get_desc_calls)
        if error is not None:
            raise error
        ctypes.memmove(destination, ctypes.byref(self.desc), ctypes.sizeof(self.desc))

    def Release(self):
        self.release_calls += 1
        assert self.release_calls == 1, "COM pointer released more than once"


def make_handler(monkeypatch, candidates, *, original_error=None):
    previous = FakeOutputPointer(
        name="OLD_OUTPUT",
        monitor=42,
        errors={1: original_error if original_error is not None else access_lost()},
    )
    # Construct the real Output wrapper without its native DPI-awareness call.
    output = Output.__new__(Output)
    output._metadata_lock = RLock()
    output.output = previous
    output.desc = DXGI_OUTPUT_DESC.from_buffer_copy(previous.desc)
    device = SimpleNamespace(enum_outputs=lambda: candidates)
    monkeypatch.setattr(
        "dxcam.runtime.output_recovery.release_com_pointer",
        lambda pointer: pointer.Release(),
    )
    return OutputRecoveryHandler(output, device), output, previous


def test_unmatched_fallback_stays_live_after_recovery(monkeypatch):
    fallback = FakeOutputPointer()
    unselected = FakeOutputPointer(name="OTHER_OUTPUT", monitor=66)
    handler, output, previous = make_handler(monkeypatch, [fallback, unselected])

    state = handler.handle(requested_region=(0, 0, 100, 100), region_set_by_user=False)

    assert output.output is fallback
    assert fallback.get_desc_calls == 2
    assert fallback.release_calls == 0
    assert previous.release_calls == unselected.release_calls == 1
    assert state.region == (0, 0, 1920, 1080)
    assert output.attached_to_desktop


@pytest.mark.parametrize("match", ["monitor", "name"])
def test_exact_match_keeps_only_selected_pointer(monkeypatch, match):
    fallback = FakeOutputPointer()
    selected = FakeOutputPointer(
        name="OLD_OUTPUT" if match == "name" else "RENAMED_OUTPUT",
        monitor=42 if match == "monitor" else 77,
    )
    unselected = FakeOutputPointer(name="OTHER_OUTPUT", monitor=66)
    handler, output, previous = make_handler(
        monkeypatch, [fallback, selected, unselected]
    )

    handler._refresh_output_desc()

    assert output.output is selected
    assert selected.release_calls == 0
    assert [pointer.release_calls for pointer in (previous, fallback, unselected)] == [
        1,
        1,
        1,
    ]


def test_later_enumeration_error_releases_candidate_not_yet_installed(monkeypatch):
    selected_by_name = FakeOutputPointer(name="OLD_OUTPUT", monitor=77)
    failure = comtypes.COMError(-2147467259, "Non-transient GetDesc failure", None)
    broken = FakeOutputPointer(errors={1: failure})
    unvisited = FakeOutputPointer(name="UNVISITED_OUTPUT", monitor=88)
    handler, output, previous = make_handler(
        monkeypatch, [selected_by_name, broken, unvisited]
    )

    with pytest.raises(comtypes.COMError) as exc:
        handler._refresh_output_desc()

    assert exc.value is failure
    assert output.output is previous
    assert previous.release_calls == 0
    assert [
        pointer.release_calls for pointer in (selected_by_name, broken, unvisited)
    ] == [1, 1, 1]


def test_refresh_error_after_transfer_leaves_replacement_owned_for_retry(monkeypatch):
    failure = access_lost()
    selected = FakeOutputPointer(errors={2: failure})
    unselected = FakeOutputPointer(name="OTHER_OUTPUT", monitor=66)
    handler, output, previous = make_handler(monkeypatch, [selected, unselected])

    with pytest.raises(comtypes.COMError) as exc:
        handler._refresh_output_desc()

    assert exc.value is failure
    assert output.output is selected
    assert selected.release_calls == 0
    assert previous.release_calls == unselected.release_calls == 1

    # A recovery retry must be able to use the installed pointer safely.
    handler._refresh_output_desc()
    assert selected.get_desc_calls == 3
    assert output.devicename == "NEW_OUTPUT"
    assert selected.release_calls == 0


def test_transient_candidate_failure_is_released_and_skipped(monkeypatch):
    unavailable = FakeOutputPointer(errors={1: access_lost()})
    fallback = FakeOutputPointer()
    handler, output, previous = make_handler(monkeypatch, [unavailable, fallback])

    handler._refresh_output_desc()

    assert output.output is fallback
    assert fallback.release_calls == 0
    assert previous.release_calls == unavailable.release_calls == 1


@pytest.mark.parametrize("empty", [False, True])
def test_no_usable_candidates_preserves_previous_output(monkeypatch, empty):
    candidates = [] if empty else [FakeOutputPointer(errors={1: access_lost()})]
    handler, output, previous = make_handler(monkeypatch, candidates)

    with pytest.raises(RuntimeError, match="No DXGI outputs available"):
        handler._refresh_output_desc()

    assert output.output is previous
    assert previous.release_calls == 0
    assert all(pointer.release_calls == 1 for pointer in candidates)


def test_nontransient_original_error_does_not_release_any_owned_pointer(monkeypatch):
    failure = comtypes.COMError(-2147467259, "Non-transient GetDesc failure", None)
    candidate = FakeOutputPointer()
    handler, output, previous = make_handler(
        monkeypatch, [candidate], original_error=failure
    )

    with pytest.raises(comtypes.COMError) as exc:
        handler._refresh_output_desc()

    assert exc.value is failure
    assert output.output is previous
    assert previous.release_calls == candidate.release_calls == 0
    assert candidate.get_desc_calls == 0
