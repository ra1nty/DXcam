from __future__ import annotations

import pytest

pytest.importorskip("comtypes")

from dxcam._libs.dxgi import (
    DXGI_ERROR_ACCESS_LOST,
    DXGI_ERROR_DEVICE_REMOVED,
    DXGI_ERROR_NOT_FOUND,
    DXGI_ERROR_SESSION_DISCONNECTED,
    DXGI_ERROR_UNSUPPORTED,
)
from dxcam.core.dxgi_errors import (
    DXGITransientContext,
    E_ACCESSDENIED,
    ProgressiveWait,
    WAIT_ABANDONED_HRESULT,
    is_transient_hresult,
)


def test_transient_error_categories_match_desktop_duplication_model() -> None:
    assert is_transient_hresult(
        DXGI_ERROR_DEVICE_REMOVED, DXGITransientContext.SYSTEM_TRANSITION
    )
    assert is_transient_hresult(
        DXGI_ERROR_ACCESS_LOST, DXGITransientContext.SYSTEM_TRANSITION
    )
    assert is_transient_hresult(
        WAIT_ABANDONED_HRESULT, DXGITransientContext.SYSTEM_TRANSITION
    )

    assert is_transient_hresult(
        E_ACCESSDENIED, DXGITransientContext.CREATE_DUPLICATION
    )
    assert is_transient_hresult(
        DXGI_ERROR_UNSUPPORTED, DXGITransientContext.CREATE_DUPLICATION
    )
    assert is_transient_hresult(
        DXGI_ERROR_SESSION_DISCONNECTED, DXGITransientContext.CREATE_DUPLICATION
    )

    assert is_transient_hresult(DXGI_ERROR_ACCESS_LOST, DXGITransientContext.FRAME_INFO)
    assert is_transient_hresult(
        DXGI_ERROR_SESSION_DISCONNECTED,
        DXGITransientContext.FRAME_INFO,
    )

    assert is_transient_hresult(DXGI_ERROR_NOT_FOUND, DXGITransientContext.ENUM_OUTPUTS)


def test_progressive_wait_bands_escalate() -> None:
    wait = ProgressiveWait()
    delays = [wait.next_delay_seconds() for _ in range(90)]

    assert delays[0] == 0.25
    assert delays[20] == 0.25
    assert delays[21] == 2.0
    assert delays[82] == 5.0
