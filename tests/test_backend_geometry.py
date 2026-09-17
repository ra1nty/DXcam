from __future__ import annotations

import ctypes
import importlib
from contextlib import contextmanager, nullcontext
from types import SimpleNamespace

import numpy as np
import pytest

from dxcam.core.winrt_duplicator import WinRTDuplicator
from dxcam.dxcam import DXCamera
from dxcam.runtime.capture_worker import CaptureWorker


def logical_pixels(width=13, height=9):
    """An asymmetric desktop image, independent of the capture-copy helpers."""
    y, x = np.indices((height, width))
    return np.stack(
        (
            (17 * x + y) % 256,
            (x + 29 * y) % 256,
            (31 * x + 7 * y) % 256,
            (13 * x + 19 * y + 101) % 256,
        ),
        axis=-1,
    ).astype(np.uint8)


class FakeOutput:
    attached_to_desktop = True
    hmonitor = 1

    def __init__(self, rotation):
        self.logical = logical_pixels()
        self.rotation_angle = rotation
        self.pending = None

    @property
    def resolution(self):
        height, width = self.logical.shape[:2]
        return width, height

    @property
    def surface_size(self):
        width, height = self.resolution
        return (height, width) if self.rotation_angle in (90, 270) else (width, height)

    def update_desc(self):
        if self.pending is not None:
            self.logical, self.rotation_angle = self.pending
            self.pending = None


class MemoryStage:
    """Fake only the GPU operations; the camera and pixel processor remain real."""

    def __init__(self, *, output, device, dim=None):
        self.rebind(output=output, device=device)
        self.releases = 0
        self.rebuild(dim=dim)

    def rebind(self, *, output, device):
        self.output = output

    def rebuild(self, dim=None):
        self.width, self.height = self.output.surface_size if dim is None else dim
        # Padding catches processors that accidentally treat row pitch as width.
        self.storage = np.full((self.height, self.width + 5, 4), 239, dtype=np.uint8)

    def ensure_size(self, *, dim):
        if (self.width, self.height) != dim:
            self.rebuild(dim)

    def copy_region_from(self, *, im_context, src_texture, src_region):
        left, top, right, bottom = src_region
        height, width = src_texture.shape[:2]
        assert 0 <= left < right <= width
        assert 0 <= top < bottom <= height
        copied = src_texture[top:bottom, left:right]
        assert copied.shape == (self.height, self.width, 4)
        self.storage[:, : self.width] = copied

    @contextmanager
    def mapped(self):
        assert self.storage is not None
        yield SimpleNamespace(
            Pitch=self.storage.strides[0], pBits=self.storage.ctypes.data
        )

    def release(self):
        if self.storage is not None:
            self.releases += 1
            self.storage = None
            self.width = self.height = 0


class MemoryDuplicator:
    def __init__(self, backend, output):
        # DXGI stores the rotated desktop in an unrotated native surface; WGC
        # supplies the desktop image already upright. Do not use runtime helpers
        # to construct either independent source image.
        angle = output.rotation_angle if backend == "dxgi" else 0
        self.texture = np.ascontiguousarray(np.rot90(output.logical, k=angle // 90))
        self.ticks = 0
        self.failed = False
        self.released = False

    @contextmanager
    def acquire_frame(self, **kwargs):
        assert not self.released
        self.ticks += 1
        yield not self.failed, not self.failed, self.ticks

    def ticks_to_seconds(self, ticks):
        return ticks / 1000

    def release(self):
        self.released = True


@pytest.fixture
def make_camera(monkeypatch):
    camera_module = importlib.import_module("dxcam.dxcam")
    monkeypatch.setattr(camera_module, "StageSurface", MemoryStage)
    monkeypatch.setattr(
        camera_module,
        "create_backend_duplicator",
        lambda backend, *, output, device: MemoryDuplicator(backend, output),
    )
    cameras = []

    def make(backend, rotation, region=None):
        output = FakeOutput(rotation)
        device = SimpleNamespace(context_guard=nullcontext, im_context=object())
        camera = DXCamera(output, device, region, output_color="BGRA", backend=backend)
        cameras.append(camera)
        return camera, output

    yield make
    for camera in cameras:
        camera.release()


def synchronous_worker(camera):
    """Run real publication cycles without starting threads or native timers."""
    with camera._DXCamera__lock:
        camera._allocate_capture_slots_for_region(camera.region, reason="test")
    worker = CaptureWorker(
        frame_buffer=camera._DXCamera__frame_buffer,
        lock=camera._DXCamera__lock,
        capture_to_stage=camera._capture_to_stage,
        get_region=camera._get_capture_region,
    )
    camera._DXCamera__worker = worker
    camera.is_capturing = True
    return worker


def expected_region(output, region):
    left, top, right, bottom = region
    return output.logical[top:bottom, left:right]


@pytest.mark.parametrize("backend", ["dxgi", "winrt"])
@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_one_shot_pixels_use_backend_geometry(make_camera, backend, rotation):
    camera, output = make_camera(backend, rotation)
    expected_size = output.surface_size if backend == "dxgi" else output.resolution
    assert (camera._stagesurf.width, camera._stagesurf.height) == expected_size
    assert camera.rotation_angle == rotation

    for region in [camera.region, (2, 1, 11, 7)]:
        expected = expected_region(output, region)
        np.testing.assert_array_equal(camera.grab(region=region), expected)
        # Exercise writing a noncontiguous destination as well as allocating.
        storage = np.full((expected.shape[0], expected.shape[1] * 2, 4), 251, np.uint8)
        destination = storage[:, ::2]
        assert camera.grab_into(destination, region=region) is True
        np.testing.assert_array_equal(destination, expected)
        assert np.all(storage[:, 1::2] == 251)


@pytest.mark.parametrize("backend", ["dxgi", "winrt"])
@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_worker_leases_keep_backend_pixel_geometry(make_camera, backend, rotation):
    camera, output = make_camera(backend, rotation)
    worker = synchronous_worker(camera)
    expected_angle = rotation if backend == "dxgi" else 0

    for region in [camera.region, (2, 1, 11, 7)]:
        camera.region = region
        with camera._DXCamera__lock:
            camera._allocate_capture_slots_for_region(region, reason="test")
        worker._run_capture_cycle()
        expected = expected_region(output, region)
        height, width = expected.shape[:2]
        expected_memory = (
            (height, width) if expected_angle in (90, 270) else (width, height)
        )
        with camera._read_lease(timeout=0) as lease:
            assert lease is not None
            assert (lease.frame_width, lease.frame_height) == (width, height)
            assert lease.rotation_angle == expected_angle
            assert (lease.stage.width, lease.stage.height) == expected_memory
        np.testing.assert_array_equal(camera.get_latest_frame(timeout=0), expected)
        destination = np.empty_like(expected)
        assert camera.get_latest_frame_into(destination, timeout=0) is True
        np.testing.assert_array_equal(destination, expected)
        assert camera.rotation_angle == rotation


@pytest.mark.parametrize("backend", ["dxgi", "winrt"])
@pytest.mark.parametrize(
    "old_rotation,new_rotation,new_size,region,expected_region_after",
    [
        (270, 90, (17, 12), None, (0, 0, 17, 12)),
        (90, 270, (8, 6), None, (0, 0, 8, 6)),
        (0, 180, (8, 6), (5, 3, 12, 8), (5, 3, 8, 6)),
    ],
    ids=["grow", "shrink", "clamp-custom-roi"],
)
def test_recovery_rebuilds_geometry_without_reinterpreting_old_lease(
    make_camera,
    backend,
    old_rotation,
    new_rotation,
    new_size,
    region,
    expected_region_after,
):
    camera, output = make_camera(backend, old_rotation, region)
    worker = synchronous_worker(camera)
    worker._run_capture_cycle()
    old_expected = expected_region(output, camera.region).copy()
    old_duplicator = camera._duplicator

    with camera._read_lease(timeout=0) as old_lease:
        assert old_lease is not None
        output.pending = logical_pixels(*new_size), new_rotation
        old_duplicator.failed = True
        worker._run_capture_cycle()  # Real camera -> output recovery -> rebuild.
        assert old_duplicator.released
        assert not camera._recovery_pending
        assert (camera.width, camera.height) == new_size
        assert camera.rotation_angle == new_rotation
        assert camera.region == expected_region_after
        expected_size = output.surface_size if backend == "dxgi" else new_size
        assert (camera._stagesurf.width, camera._stagesurf.height) == expected_size
        expected_angle = new_rotation if backend == "dxgi" else 0
        for slot in camera._DXCamera__frame_buffer.slots:
            assert slot.rotation_angle == expected_angle
        worker._run_capture_cycle()
        np.testing.assert_array_equal(
            camera.get_latest_frame(timeout=0), expected_region(output, camera.region)
        )
        assert old_lease.slot.retired
        assert old_lease.stage.releases == 0
        np.testing.assert_array_equal(
            camera._process_stage(
                stage=old_lease.stage,
                frame_width=old_lease.frame_width,
                frame_height=old_lease.frame_height,
                rotation_angle=old_lease.rotation_angle,
            ),
            old_expected,
        )
    assert old_lease.stage.releases == 1
    camera.stop()
    expected = expected_region(output, camera.region)
    np.testing.assert_array_equal(camera.grab(), expected)
    destination = np.empty_like(expected)
    assert camera.grab_into(destination) is True
    np.testing.assert_array_equal(destination, expected)


def size_checker(rotation, pool_dimensions):
    duplicator = WinRTDuplicator.__new__(WinRTDuplicator)
    duplicator._output = FakeOutput(rotation)
    duplicator._frame_pool_dimensions = pool_dimensions
    return duplicator


def content_frame(size):
    return SimpleNamespace(content_size=SimpleNamespace(width=size[0], height=size[1]))


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_winrt_accepts_upright_content_for_rotated_monitor(rotation):
    duplicator = size_checker(rotation, (13, 9))
    assert not duplicator._frame_size_mismatch(content_frame((13, 9)))
    assert duplicator._frame_size_mismatch(content_frame((17, 12)))
    assert duplicator._frame_size_mismatch(content_frame((8, 6)))


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_winrt_rejects_old_pool_after_shared_output_already_refreshed(rotation):
    # ContentSize and shared Output now agree, but the old pool would clip the
    # larger frame. It must still take the coordinated camera recovery path.
    duplicator = size_checker(rotation, (8, 6))
    assert duplicator._frame_size_mismatch(content_frame((13, 9)))


def test_winrt_records_actual_capture_item_pool_size(monkeypatch):
    module = importlib.import_module("dxcam.core.winrt_duplicator")
    monkeypatch.delenv("DXCAM_WINRT_DIRTY_REGION_MODE", raising=False)
    monkeypatch.setattr(WinRTDuplicator, "_configure_qpc_frequency", lambda self: None)
    events = []
    item_size = SimpleNamespace(width=17, height=12)
    item = SimpleNamespace(size=item_size)
    session = SimpleNamespace(
        start_capture=lambda: events.append("start"),
        close=lambda: events.append("session-close"),
    )
    pool = SimpleNamespace(
        create_capture_session=lambda capture_item: session,
        add_frame_arrived=lambda handler: 1,
        remove_frame_arrived=lambda token: None,
        close=lambda: events.append("pool-close"),
    )

    def create_pool(device, pixel_format, buffer_count, size):
        assert size is item_size
        events.append("create")
        return pool

    bindings = SimpleNamespace(
        get_dxgi_surface_from_object=None,
        dirty_region_mode_enum=None,
        directx_pixel_format=SimpleNamespace(B8_G8_R8_A8_UINT_NORMALIZED=87),
        create_direct3d11_device_from_dxgi_device=lambda pointer: object(),
        create_for_monitor=lambda monitor: item,
        frame_pool_cls=SimpleNamespace(create_free_threaded=create_pool),
    )
    monkeypatch.setattr(module._WinRTCaptureBindings, "load", lambda: bindings)
    device = SimpleNamespace(
        device=SimpleNamespace(QueryInterface=lambda interface: ctypes.c_void_p(1234))
    )
    # Deliberately different from Output: the pool must remember its actual
    # creation size, including a display transition during session creation.
    duplicator = WinRTDuplicator(output=FakeOutput(90), device=device)
    try:
        assert duplicator._frame_pool_dimensions == (17, 12)
        assert events == ["create", "start"]
    finally:
        duplicator.release()
    assert duplicator._frame_pool_dimensions is None
    assert events[-2:] == ["session-close", "pool-close"]
