from __future__ import annotations

from types import SimpleNamespace

import comtypes
import pytest

import dxcam.core.dxgi_duplicator as dxgi_module
import dxcam.core.winrt_duplicator as winrt_module
from dxcam._libs.dxgi import DXGI_ERROR_ACCESS_LOST, DXGI_ERROR_WAIT_TIMEOUT
from dxcam.core.dxgi_duplicator import DXGIDuplicator
from dxcam.core.duplicator_protocol import FrameDuplicator
from dxcam.core.winrt_duplicator import WinRTDuplicator


def com_error(code):
    return comtypes.COMError(code, "fake COM failure", None)


class FakeReference:
    def __init__(self, valid=True):
        self.valid = valid
        self.releases = 0

    def __bool__(self):
        return self.valid

    def release(self):
        if self.valid:
            self.releases += 1
            self.valid = False


class FakeResource(FakeReference):
    def __init__(self):
        super().__init__(valid=False)
        self.queries = 0
        self.query_error = None
        self.texture = None

    def QueryInterface(self, interface):
        self.queries += 1
        if self.query_error is not None:
            raise self.query_error
        self.texture = FakeReference()
        return self.texture


@pytest.fixture
def make_dxgi(monkeypatch):
    def make(frames):
        resources, timeouts, released_frames = [], [], []
        pending = iter(frames)
        current = {}

        def resource():
            result = FakeResource()
            resources.append(result)
            return result

        def pointer_type(interface):
            if interface is dxgi_module.IDXGIResource:
                return resource
            assert interface is dxgi_module.ID3D11Texture2D
            return lambda: None

        def acquire(timeout, info, result):
            nonlocal current
            timeouts.append(timeout)
            current = next(pending)
            if "acquire_error" in current:
                raise current["acquire_error"]
            info.LastPresentTime = current.get("present", 0)
            info.LastMouseUpdateTime = current.get("mouse", 0)
            info.AccumulatedFrames = current.get("accumulated", 1)
            result.valid = True
            result.query_error = current.get("query_error")

        def release_frame():
            released_frames.append(current)
            if "release_error" in current:
                raise current["release_error"]

        monkeypatch.setattr(
            dxgi_module,
            "ctypes",
            SimpleNamespace(POINTER=pointer_type, byref=lambda value: value),
        )
        monkeypatch.setattr(
            dxgi_module,
            "release_com_pointer",
            lambda pointer: pointer.release() if pointer is not None else None,
        )
        duplicator = DXGIDuplicator.__new__(DXGIDuplicator)
        duplicator.duplicator = SimpleNamespace(
            AcquireNextFrame=acquire, ReleaseFrame=release_frame
        )
        duplicator.texture = None
        duplicator.updated = False
        duplicator._frame_held = False
        duplicator._has_frame = False
        duplicator.latest_frame_ticks = 0
        duplicator.performance_frequency = 10_000_000
        return SimpleNamespace(
            duplicator=duplicator,
            resources=resources,
            timeouts=timeouts,
            released_frames=released_frames,
        )

    return make


@pytest.mark.parametrize("timeout_ms,expected", [(None, 0), (0, 0), (17, 17)])
@pytest.mark.parametrize("wait_for_frame", [False, True])
def test_dxgi_forwards_explicit_wait_and_preserves_default_poll(
    make_dxgi, timeout_ms, expected, wait_for_frame
):
    fake = make_dxgi([{"present": 100}])
    with fake.duplicator.acquire_frame(wait_for_frame, timeout_ms=timeout_ms) as frame:
        assert frame == (True, True, 100)
        assert fake.duplicator._frame_held
        assert fake.resources[0].releases == 1
    assert fake.timeouts == [expected]
    assert len(fake.released_frames) == 1
    assert fake.resources[0].texture.releases == 1
    assert not fake.duplicator._frame_held


def test_dxgi_pointer_updates_do_not_change_image_or_timestamp_after_seed(make_dxgi):
    fake = make_dxgi(
        [
            {"present": 100},
            {"mouse": 200},
            {"mouse": 300},
            {"present": 400, "mouse": 450},
        ]
    )
    frames = []
    for _ in range(4):
        with fake.duplicator.acquire_frame(timeout_ms=10) as frame:
            frames.append(frame)
            if not frame[1]:
                assert fake.duplicator.accumulated_frames == 0
                assert not fake.duplicator._frame_held
    assert frames == [
        (True, True, 100),
        (True, False, 100),
        (True, False, 100),
        (True, True, 400),
    ]
    assert [resource.queries for resource in fake.resources] == [1, 0, 0, 1]
    assert [resource.releases for resource in fake.resources] == [1, 1, 1, 1]
    assert len(fake.released_frames) == 4


def test_dxgi_first_pointer_only_frame_seeds_static_desktop(make_dxgi):
    fake = make_dxgi([{"mouse": 50}, {"mouse": 75}, {"present": 100}])
    expected = [(True, True, 50), (True, False, 50), (True, True, 100)]
    for result in expected:
        with fake.duplicator.acquire_frame() as frame:
            assert frame == result
    assert [resource.queries for resource in fake.resources] == [1, 0, 1]
    assert len(fake.released_frames) == 3


def test_dxgi_reset_reseeds_static_desktop_after_capture_restart(make_dxgi):
    fake = make_dxgi(
        [
            {"present": 100},
            {"mouse": 200},
            {"mouse": 300},
            {"mouse": 400},
        ]
    )
    with fake.duplicator.acquire_frame() as frame:
        assert frame == (True, True, 100)
    with fake.duplicator.acquire_frame() as frame:
        assert frame == (True, False, 100)
    fake.duplicator.reset_frame_tracking()
    with fake.duplicator.acquire_frame() as frame:
        assert frame == (True, True, 300)
    with fake.duplicator.acquire_frame() as frame:
        assert frame == (True, False, 300)
    assert [resource.queries for resource in fake.resources] == [1, 0, 1, 0]
    assert [resource.releases for resource in fake.resources] == [1, 1, 1, 1]
    assert len(fake.released_frames) == 4


def test_dxgi_seed_without_native_timestamp_uses_current_qpc_time(
    make_dxgi, monkeypatch
):
    fake = make_dxgi([{}])
    monkeypatch.setattr(dxgi_module, "time", SimpleNamespace(perf_counter=lambda: 1.25))
    with fake.duplicator.acquire_frame() as frame:
        assert frame == (True, True, 12_500_000)


def test_failed_texture_query_does_not_prevent_later_pointer_seed(make_dxgi):
    fake = make_dxgi(
        [
            {"present": 100, "query_error": com_error(-2147467262)},
            {"mouse": 200},
        ]
    )
    with fake.duplicator.acquire_frame() as frame:
        assert frame == (True, False, 0)
    assert not fake.duplicator._has_frame
    with fake.duplicator.acquire_frame() as frame:
        assert frame == (True, True, 200)
    assert len(fake.released_frames) == 2
    assert [resource.releases for resource in fake.resources] == [1, 1]


@pytest.mark.parametrize("failure_location", ["query", "caller"])
def test_dxgi_releases_acquired_frame_when_query_or_caller_raises(
    make_dxgi, failure_location
):
    spec = {"present": 100}
    if failure_location == "query":
        spec["query_error"] = RuntimeError("conversion failed")
    fake = make_dxgi([spec])
    with pytest.raises(RuntimeError, match="conversion failed"):
        with fake.duplicator.acquire_frame():
            raise RuntimeError("conversion failed")
    assert not fake.duplicator._frame_held
    assert len(fake.released_frames) == 1
    assert fake.resources[0].releases == 1
    if fake.resources[0].texture is not None:
        assert fake.resources[0].texture.releases == 1


@pytest.mark.parametrize(
    "code,expected_ok",
    [(DXGI_ERROR_WAIT_TIMEOUT, True), (DXGI_ERROR_ACCESS_LOST, False)],
)
def test_dxgi_timeout_and_access_loss_do_not_release_unacquired_frames(
    make_dxgi, code, expected_ok
):
    fake = make_dxgi([{"acquire_error": com_error(code)}])
    with fake.duplicator.acquire_frame(timeout_ms=25) as frame:
        assert frame == (expected_ok, False, 0)
    assert fake.timeouts == [25]
    assert not fake.released_frames
    assert fake.resources[0].releases == 0


def test_pointer_only_release_access_loss_requests_recovery_once(make_dxgi):
    fake = make_dxgi(
        [
            {"present": 100},
            {"mouse": 200, "release_error": com_error(DXGI_ERROR_ACCESS_LOST)},
        ]
    )
    with fake.duplicator.acquire_frame():
        pass
    with fake.duplicator.acquire_frame() as frame:
        assert frame == (False, False, 100)
    assert len(fake.released_frames) == 2
    assert [resource.releases for resource in fake.resources] == [1, 1]
    assert not fake.duplicator._frame_held


@pytest.mark.parametrize(
    "failure_location", ["acquire", "image_release", "pointer_release"]
)
def test_dxgi_propagates_fatal_com_errors_without_double_release(
    make_dxgi, failure_location
):
    failure = com_error(-2147024809)
    if failure_location == "acquire":
        specs = [{"acquire_error": failure}]
    elif failure_location == "image_release":
        specs = [{"present": 100, "release_error": failure}]
    else:
        specs = [{"present": 100}, {"mouse": 200, "release_error": failure}]
    fake = make_dxgi(specs)
    if failure_location == "pointer_release":
        with fake.duplicator.acquire_frame():
            pass
    with pytest.raises(comtypes.COMError):
        with fake.duplicator.acquire_frame():
            pass
    assert not fake.duplicator._frame_held
    expected_releases = 0 if failure_location == "acquire" else len(specs)
    assert len(fake.released_frames) == expected_releases
    assert sum(resource.releases for resource in fake.resources) == expected_releases


def make_winrt():
    waits, clears, drains = [], [], []
    duplicator = WinRTDuplicator.__new__(WinRTDuplicator)
    duplicator._frame = None
    duplicator._dxgi_surface = None
    duplicator._frame_wait_seconds = 0.002
    duplicator._frame_arrived_event = SimpleNamespace(
        wait=lambda timeout: waits.append(timeout) or True,
        clear=lambda: clears.append(True),
    )

    def drain():
        drains.append(True)
        return None, 0, False

    duplicator._drain_to_latest_frame = drain
    return SimpleNamespace(
        duplicator=duplicator, waits=waits, clears=clears, drains=drains
    )


@pytest.mark.parametrize(
    "wait_for_frame,timeout_ms,waits",
    [
        (False, None, []),
        (True, None, [0.002]),
        (True, 0, []),
        (False, 10, [0.01]),
        (True, 25, [0.025]),
    ],
)
def test_winrt_explicit_timeout_overrides_default_wait_policy(
    wait_for_frame, timeout_ms, waits
):
    fake = make_winrt()
    with fake.duplicator.acquire_frame(wait_for_frame, timeout_ms=timeout_ms) as frame:
        assert frame == (True, False, 0)
    assert fake.waits == waits
    assert len(fake.drains) == (2 if waits else 1)
    assert len(fake.clears) == len(waits)


@pytest.mark.parametrize("timeout_ms,waits", [(None, []), (0, []), (12, [0.012])])
def test_winrt_missing_event_still_honors_explicit_wait(monkeypatch, timeout_ms, waits):
    fake = make_winrt()
    fake.duplicator._frame_arrived_event = None
    slept = []
    monkeypatch.setattr(winrt_module, "time", SimpleNamespace(sleep=slept.append))
    with fake.duplicator.acquire_frame(True, timeout_ms=timeout_ms) as frame:
        assert frame == (True, False, 0)
    assert slept == waits
    assert len(fake.drains) == (2 if waits else 1)


def test_winrt_available_frame_does_not_wait_again():
    fake = make_winrt()
    frame = object()
    fake.duplicator._drain_to_latest_frame = lambda: (frame, 1, False)
    fake.duplicator._frame_size_mismatch = lambda _: True
    fake.duplicator._handle_frame_size_change = lambda _: True
    with fake.duplicator.acquire_frame(timeout_ms=100):
        pass
    assert not fake.waits


@pytest.mark.parametrize("failure_after_wait", [False, True])
def test_winrt_acquisition_failure_requests_recovery(failure_after_wait):
    fake = make_winrt()
    results = (
        [(None, 0, False), (None, 0, True)] if failure_after_wait else [(None, 0, True)]
    )
    pending = iter(results)
    fake.duplicator._drain_to_latest_frame = lambda: next(pending)
    with fake.duplicator.acquire_frame(timeout_ms=10) as frame:
        assert frame == (False, False, 0)
    assert fake.waits == ([0.01] if failure_after_wait else [])


def test_backends_implement_tracking_reset_contract(make_dxgi):
    dxgi = make_dxgi([]).duplicator
    winrt = make_winrt().duplicator
    winrt.texture = None
    assert isinstance(dxgi, FrameDuplicator)
    assert isinstance(winrt, FrameDuplicator)
    winrt.latest_frame_ticks = 123
    winrt.reset_frame_tracking()
    assert winrt.latest_frame_ticks == 123
