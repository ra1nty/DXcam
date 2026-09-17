from __future__ import annotations

from collections import deque
from types import SimpleNamespace

import pytest

from dxcam.core.winrt_duplicator import WinRTDuplicator
from dxcam.dxcam import DXCamera


class FakeFrame:
    def __init__(self, size, name, events):
        self.content_size = SimpleNamespace(width=size[0], height=size[1])
        self.name = name
        self.events = events
        self.closes = 0

    def close(self):
        self.closes += 1
        self.events.append(self.name)


@pytest.fixture
def make_duplicator(monkeypatch):
    monkeypatch.delenv("DXCAM_WINRT_DIRTY_REGION_MODE", raising=False)
    monkeypatch.setattr(WinRTDuplicator, "_configure_qpc_frequency", lambda self: None)
    monkeypatch.setattr(
        WinRTDuplicator, "_create_capture_session", lambda *a, **kw: None
    )

    def make(old_size, frames):
        queue = deque(frames)
        mutations = []
        output = SimpleNamespace(
            surface_size=old_size,
            resolution=old_size,
            update_desc=lambda: mutations.append("output"),
        )
        duplicator = WinRTDuplicator(output=output, device=object())
        duplicator._frame_pool = SimpleNamespace(
            try_get_next_frame=lambda: queue.popleft() if queue else None,
            recreate=lambda *a: mutations.append("pool"),
            close=lambda: None,
        )
        return duplicator, mutations

    return make


@pytest.mark.parametrize("new_size", [(1280, 720), (2560, 1440)])
@pytest.mark.parametrize("previous_frame_held", [False, True])
def test_size_change_closes_frames_and_routes_to_camera_recovery(
    make_duplicator, new_size, previous_frame_held
):
    events = []
    old_size = (1920, 1080)
    dropped = FakeFrame(old_size, "dropped", events)
    changed = FakeFrame(new_size, "changed", events)
    duplicator, mutations = make_duplicator(old_size, [dropped, changed])
    duplicator.updated = True
    duplicator.accumulated_frames = 3
    held = FakeFrame(old_size, "held", events)
    if previous_frame_held:
        duplicator._frame = held

    def recover():
        assert changed.closes == 1
        assert not mutations
        assert not duplicator.updated
        assert duplicator.accumulated_frames == 0
        events.append("recovery")
        duplicator.release()

    # Use the real acquisition and size-handler paths. Any attempted GPU copy
    # fails because this camera intentionally has no device or staging texture.
    camera = SimpleNamespace(
        _duplicator=duplicator,
        _recover_output=recover,
        backend="winrt",
        width=old_size[0],
        height=old_size[1],
        rotation_angle=0,
        _capture_rotation_angle=0,
    )
    result = DXCamera._capture_to_stage(
        camera, (0, 0, *old_size), object(), timeout_ms=0
    )

    assert result == (False, 0, 0, 0, 0)
    assert events == (["held"] if previous_frame_held else []) + [
        "dropped",
        "changed",
        "recovery",
    ]
    duplicator.release()
    assert changed.closes == dropped.closes == 1
    assert held.closes == int(previous_frame_held)
    assert not mutations


def test_changed_frame_is_closed_even_when_camera_recovery_fails(make_duplicator):
    events = []
    changed = FakeFrame((1280, 720), "changed", events)
    duplicator, _ = make_duplicator((1920, 1080), [changed])

    def recover():
        assert changed.closes == 1
        raise RuntimeError("recovery failed")

    camera = SimpleNamespace(
        _duplicator=duplicator,
        _recover_output=recover,
        backend="winrt",
        width=1920,
        height=1080,
        rotation_angle=0,
        _capture_rotation_angle=0,
    )
    with pytest.raises(RuntimeError, match="recovery failed"):
        DXCamera._capture_to_stage(camera, (0, 0, 1920, 1080), object(), timeout_ms=0)
    duplicator.release()
    assert changed.closes == 1
    assert not duplicator.updated


@pytest.mark.parametrize("setting", ["report_and_render", " REPORT-AND-RENDER "])
def test_partial_frame_mode_fails_before_native_setup(monkeypatch, setting):
    monkeypatch.setenv("DXCAM_WINRT_DIRTY_REGION_MODE", setting)

    def unexpected_setup(*args, **kwargs):
        pytest.fail("Invalid frame mode reached native setup")

    monkeypatch.setattr(WinRTDuplicator, "_configure_qpc_frequency", unexpected_setup)
    monkeypatch.setattr(WinRTDuplicator, "_create_capture_session", unexpected_setup)
    with pytest.raises(ValueError, match="requires complete frames.*report_only"):
        WinRTDuplicator(output=object(), device=object())


@pytest.mark.parametrize("setting", [None, "default", "report_only", " REPORT-ONLY "])
def test_complete_frame_modes_remain_supported(monkeypatch, setting):
    if setting is None:
        monkeypatch.delenv("DXCAM_WINRT_DIRTY_REGION_MODE", raising=False)
    else:
        monkeypatch.setenv("DXCAM_WINRT_DIRTY_REGION_MODE", setting)
    monkeypatch.setattr(WinRTDuplicator, "_configure_qpc_frequency", lambda self: None)
    monkeypatch.setattr(
        WinRTDuplicator, "_create_capture_session", lambda *a, **kw: None
    )
    duplicator = WinRTDuplicator(output=object(), device=object())
    report_only = object()
    duplicator._dirty_region_mode_enum = SimpleNamespace(REPORT_ONLY=report_only)
    duplicator._session = SimpleNamespace()
    duplicator._apply_session_options()

    if setting is None or setting == "default":
        assert not hasattr(duplicator._session, "dirty_region_mode")
    else:
        assert duplicator._session.dirty_region_mode is report_only
