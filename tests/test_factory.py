from __future__ import annotations

import gc
import signal
import subprocess
import sys
import textwrap
import weakref
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("comtypes")

import dxcam


class FakeOutput:
    def __init__(self, name: str) -> None:
        self.devicename = name
        self.resolution = (1920, 1080)
        self.rotation_angle = 0

    def update_desc(self) -> None:
        pass


class FakeDevice:
    def __init__(self, adapter: str) -> None:
        self.adapter = adapter

    def enum_outputs(self) -> list[str]:
        return ["secondary", "primary"]

    def __repr__(self) -> str:
        return self.adapter


class FakeCamera:
    def __init__(self, **kwargs: Any) -> None:
        self.options = kwargs
        self.is_released = False

    def release(self) -> None:
        self.is_released = True


@pytest.fixture
def fake_hardware(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    discovery_calls: list[None] = []
    camera_calls: list[dict[str, Any]] = []

    def discover() -> list[str]:
        discovery_calls.append(None)
        return ["fake adapter"]

    def camera(**kwargs: Any) -> FakeCamera:
        camera_calls.append(kwargs)
        return FakeCamera(**kwargs)

    monkeypatch.setattr(dxcam, "__factory", None)
    monkeypatch.setattr(dxcam, "enum_dxgi_adapters", discover)
    monkeypatch.setattr(
        dxcam,
        "get_output_metadata",
        lambda: {
            "primary": ["monitor", True, []],
            "secondary": ["monitor", False, []],
        },
    )
    monkeypatch.setattr(dxcam, "Device", FakeDevice)
    monkeypatch.setattr(dxcam, "Output", FakeOutput)
    monkeypatch.setattr(dxcam, "DXCamera", camera)
    monkeypatch.setattr(dxcam, "time", SimpleNamespace(sleep=lambda _: None))
    # These tests must not replace the process's actual SIGTERM handler.
    monkeypatch.setattr(dxcam, "_install_sigterm_handler", lambda: None)
    return SimpleNamespace(discovery=discovery_calls, cameras=camera_calls)


def test_fresh_import_does_not_discover_devices_or_change_process_settings() -> None:
    script = textwrap.dedent("""
        import ctypes
        import logging
        import signal

        def fail(*args, **kwargs):
            raise AssertionError("Import must not discover or initialize hardware")

        ctypes.windll.dxgi.CreateDXGIFactory1 = fail
        ctypes.windll.d3d11.D3D11CreateDevice = fail
        ctypes.windll.user32.EnumDisplayDevicesW = fail
        loggers = [logging.getLogger("comtypes"),
                   logging.getLogger("comtypes._post_coinit.unknwn")]
        for logger in loggers:
            logger.setLevel(logging.WARNING)
        previous_handler = signal.getsignal(signal.SIGTERM)

        import dxcam

        assert all(logger.level == logging.WARNING for logger in loggers)
        assert signal.getsignal(signal.SIGTERM) == previous_handler
    """)
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("first_call", ["create", "device_info", "output_info"])
def test_public_entry_points_share_lazy_discovery(
    fake_hardware: SimpleNamespace, first_call: str
) -> None:
    assert fake_hardware.discovery == []
    getattr(dxcam, first_call)()
    camera = dxcam.create()
    assert camera.options["output"].devicename == "primary"
    assert dxcam.device_info() == "Device[0]:fake adapter\n"
    assert "Output[1]: Res:(1920, 1080) Rot:0 Primary:True" in dxcam.output_info()
    assert len(fake_hardware.discovery) == 1


def test_camera_reuse_and_recreation_after_release(
    fake_hardware: SimpleNamespace,
) -> None:
    first = dxcam.create(output_idx=0, output_color="RGB")
    assert dxcam.create(output_idx=0, output_color="BGR") is first
    assert first.options["output_color"] == "RGB"
    first.release()
    replacement = dxcam.create(output_idx=0, output_color="BGR")
    assert replacement is not first
    assert replacement.options["output_color"] == "BGR"
    assert len(fake_hardware.cameras) == 2


def test_camera_cache_separates_outputs_and_backends(
    fake_hardware: SimpleNamespace,
) -> None:
    secondary = dxcam.create(output_idx=0)
    primary = dxcam.create(output_idx=1)
    winrt = dxcam.create(output_idx=0, backend="winrt")
    assert len({id(secondary), id(primary), id(winrt)}) == 3
    assert dxcam.create(output_idx=0) is secondary
    assert dxcam.create(output_idx=1) is primary
    assert dxcam.create(output_idx=0, backend="winrt") is winrt


def test_camera_cache_does_not_keep_unused_cameras_alive(
    fake_hardware: SimpleNamespace,
) -> None:
    camera = dxcam.create(output_idx=0)
    reference = weakref.ref(camera)
    del camera
    gc.collect()
    assert reference() is None
    assert dxcam.create(output_idx=0) is not None
    assert len(fake_hardware.cameras) == 2


def test_factories_do_not_share_camera_caches(fake_hardware: SimpleNamespace) -> None:
    first = dxcam.DXFactory()
    second = dxcam.DXFactory()
    assert first.create(output_idx=0) is not second.create(output_idx=0)


def test_concurrent_first_creation_discovers_once_and_reuses_camera(
    fake_hardware: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    workers = 8
    start = Barrier(workers + 1)
    discovering = Event()
    allow_discovery = Event()
    discover = dxcam.enum_dxgi_adapters

    def held_discovery() -> list[str]:
        result = discover()
        discovering.set()
        assert allow_discovery.wait(timeout=10)
        return result

    def create() -> Any:
        start.wait(timeout=10)
        return dxcam.create(output_idx=0)

    monkeypatch.setattr(dxcam, "enum_dxgi_adapters", held_discovery)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(create) for _ in range(workers)]
        try:
            start.wait(timeout=10)
            assert discovering.wait(timeout=10)
        finally:
            allow_discovery.set()
        cameras = [future.result(timeout=10) for future in futures]
    assert all(camera is cameras[0] for camera in cameras)
    assert len(fake_hardware.discovery) == 1
    assert len(fake_hardware.cameras) == 1


def test_sigterm_before_initialization_does_not_discover_hardware(
    fake_hardware: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dxcam, "_previous_sigterm_handler", None)
    dxcam._handle_sigterm(signal.SIGTERM, None)
    assert fake_hardware.discovery == []


def test_failed_signal_install_does_not_overwrite_previous_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_install(signum: int, handler: Any) -> None:
        raise ValueError("signal only works in main thread")

    monkeypatch.setattr(dxcam, "_sigterm_handler_installed", False)
    monkeypatch.setattr(dxcam, "_previous_sigterm_handler", None)
    monkeypatch.setattr(
        dxcam,
        "signal",
        SimpleNamespace(
            SIGTERM=signal.SIGTERM,
            getsignal=lambda _: dxcam._handle_sigterm,
            signal=fail_install,
        ),
    )
    dxcam._install_sigterm_handler()
    assert dxcam._previous_sigterm_handler is None
    assert not dxcam._sigterm_handler_installed


def test_sigterm_releases_cached_cameras_and_calls_previous_handler(
    fake_hardware: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    cameras = [dxcam.create(output_idx=0), dxcam.create(output_idx=1)]
    handled: list[int] = []

    def previous(signum: int, frame: Any) -> None:
        assert all(camera.is_released for camera in cameras)
        handled.append(signum)

    monkeypatch.setattr(dxcam, "_previous_sigterm_handler", previous)
    dxcam._handle_sigterm(signal.SIGTERM, None)
    assert handled == [signal.SIGTERM]
    assert dxcam.create(output_idx=0) not in cameras


def test_removed_buffer_length_fails_without_discovery(
    fake_hardware: SimpleNamespace,
) -> None:
    with pytest.raises(TypeError):
        dxcam.create(max_buffer_len=8)
    with pytest.raises(TypeError):
        dxcam.create(0, 0, None, "RGB", 8)
    assert fake_hardware.discovery == []
