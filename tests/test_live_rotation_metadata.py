from __future__ import annotations

import ctypes
from contextlib import contextmanager
from threading import Event, RLock, Thread, current_thread
from types import SimpleNamespace

import comtypes
import numpy as np
import pytest

from dxcam._libs.dxgi import DXGI_ERROR_ACCESS_LOST, DXGI_OUTPUT_DESC
from dxcam.core.output import Output
from dxcam.dxcam import DXCamera
from dxcam.runtime.output_recovery import OutputRecoveryHandler
from test_backend_geometry import make_camera as make_camera


class NativeOutput:
    def __init__(self, rotation=1):
        self.desc = DXGI_OUTPUT_DESC()
        self.desc.DeviceName = "DISPLAY_FOR_ROTATION_TEST"
        self.desc.Monitor = 42
        self.desc.AttachedToDesktop = True
        self.desc.DesktopCoordinates.right = 1920
        self.desc.DesktopCoordinates.bottom = 1080
        self.desc.Rotation = rotation
        self.calls = 0
        self.releases = 0
        self.error = None
        self.hook = None

    def GetDesc(self, destination):
        self.calls += 1
        if self.hook is not None:
            self.hook(self.calls)
        assert self.releases == 0, "Native query used a released output"
        if self.error is not None:
            raise self.error
        ctypes.memmove(destination, ctypes.byref(self.desc), ctypes.sizeof(self.desc))

    def Release(self):
        self.releases += 1
        assert self.releases == 1


def make_output(native):
    # Bypass only the native DPI initialization; retain real metadata methods.
    output = Output.__new__(Output)
    output.output = native
    output.desc = DXGI_OUTPUT_DESC.from_buffer_copy(native.desc)
    output._metadata_lock = RLock()
    return output


@contextmanager
def metadata_camera(output, backend="winrt"):
    camera = DXCamera.__new__(DXCamera)
    camera._is_released = False
    camera.backend = backend
    camera._output = output
    camera.rotation_angle = output.rotation_angle
    try:
        yield camera
    finally:
        # This camera owns no capture resources.
        camera._is_released = True


def test_standalone_winrt_reads_live_rotation_without_mutating_shared_geometry():
    native = NativeOutput()
    output = make_output(native)
    cached_desc = bytes(output.desc)
    with metadata_camera(output) as camera:
        native.desc.Rotation = 3  # Same-size 180-degree transition, no recovery.
        assert camera.rotation_angle == 180
        assert camera._capture_rotation_angle == 0
        assert camera._rotation_angle == 180
        assert bytes(output.desc) == cached_desc
        assert output.rotation_angle == 0
        assert output.resolution == (1920, 1080)
        assert native.calls == 1

        # A later explicit read must query again, independent of other cameras.
        native.desc.Rotation = 1
        assert camera.rotation_angle == 0
        assert native.calls == 2


@pytest.mark.parametrize(
    "error",
    [
        OSError("Unavailable display"),
        comtypes.COMError(DXGI_ERROR_ACCESS_LOST, "", None),
    ],
)
def test_winrt_query_failure_keeps_last_successful_rotation(error):
    native = NativeOutput()
    output = make_output(native)
    with metadata_camera(output) as camera:
        native.desc.Rotation = 3
        assert camera.rotation_angle == 180
        native.error = error
        assert camera.rotation_angle == 180
        assert camera._capture_rotation_angle == 0
        assert output.rotation_angle == 0
        assert native.calls == 2


def test_released_winrt_camera_returns_cache_without_querying_output():
    native = NativeOutput()
    with metadata_camera(make_output(native)) as camera:
        native.desc.Rotation = 3
        assert camera.rotation_angle == 180
        camera._is_released = True
        native.error = AssertionError("Released camera queried native output")
        assert camera.rotation_angle == 180
        assert native.calls == 1


def test_dxgi_public_and_capture_rotation_remain_cached_and_assignable():
    native = NativeOutput()
    with metadata_camera(make_output(native), backend="dxgi") as camera:
        native.desc.Rotation = 3
        native.error = AssertionError("DXGI cached rotation queried native output")
        assert camera.rotation_angle == 0
        camera.rotation_angle = 90
        assert camera.rotation_angle == camera._capture_rotation_angle == 90
        assert native.calls == 0


def test_winrt_frame_processing_does_not_query_or_apply_live_monitor_rotation(
    make_camera, monkeypatch
):
    camera, output = make_camera("winrt", 0)
    queries = []

    def current_rotation():
        queries.append(True)
        return 180

    monkeypatch.setattr(output, "read_current_rotation", current_rotation)
    for _ in range(3):
        np.testing.assert_array_equal(camera.grab(), output.logical)
    assert not queries

    assert camera.rotation_angle == 180
    for _ in range(3):
        np.testing.assert_array_equal(camera.grab(), output.logical)
    assert len(queries) == 1


def test_winrt_recovery_logging_uses_cached_rotation(make_camera, monkeypatch):
    camera, output = make_camera("winrt", 0)

    def unexpected():
        pytest.fail("Recovery performed an extra live metadata query")

    monkeypatch.setattr(output, "read_current_rotation", unexpected)
    output.pending = (output.logical, 180)
    camera._recover_output()
    assert camera._rotation_angle == 180
    assert camera._capture_rotation_angle == 0
    np.testing.assert_array_equal(camera.grab(), output.logical)


def test_recovery_cannot_release_output_during_an_inflight_rotation_query(monkeypatch):
    original = NativeOutput(rotation=3)
    replacement = NativeOutput(rotation=2)
    output = make_output(original)
    query_entered, finish_query, recovery_attempted = Event(), Event(), Event()

    class ObservedLock:
        def __init__(self):
            self.lock = RLock()

        def __enter__(self):
            if current_thread().name == "rotation-recovery":
                recovery_attempted.set()
            return self.lock.__enter__()

        def __exit__(self, *args):
            return self.lock.__exit__(*args)

    output._metadata_lock = ObservedLock()

    def query_hook(call):
        if call == 1:
            query_entered.set()
            assert finish_query.wait(5)
        else:
            # Force the real fallback selection/replacement path after the read.
            raise comtypes.COMError(DXGI_ERROR_ACCESS_LOST, "Transition", None)

    original.hook = query_hook
    handler = OutputRecoveryHandler(
        output, SimpleNamespace(enum_outputs=lambda: [replacement])
    )
    monkeypatch.setattr(
        "dxcam.runtime.output_recovery.release_com_pointer", lambda ptr: ptr.Release()
    )
    results, errors = [], []

    def read():
        try:
            results.append(output.read_current_rotation())
        except BaseException as error:
            errors.append(error)

    def recover():
        try:
            handler._refresh_output_desc()
        except BaseException as error:
            errors.append(error)

    reader = Thread(target=read, name="rotation-reader")
    recovery = Thread(target=recover, name="rotation-recovery")
    reader.start()
    try:
        assert query_entered.wait(5)
        recovery.start()
        assert recovery_attempted.wait(5)
        assert original.calls == 1
        assert original.releases == 0
        assert output.output is original
    finally:
        finish_query.set()
        reader.join(5)
        if recovery.ident is not None:
            recovery.join(5)
    assert not reader.is_alive() and not recovery.is_alive()
    assert not errors
    assert results == [180]
    assert output.output is replacement
    assert original.releases == 1
    assert replacement.releases == 0
    assert output.read_current_rotation() == 90
