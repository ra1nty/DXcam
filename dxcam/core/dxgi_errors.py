from __future__ import annotations

import ctypes
import time
from enum import Enum

import comtypes

from dxcam._libs.dxgi import (
    DXGI_ERROR_ACCESS_LOST,
    DXGI_ERROR_DEVICE_REMOVED,
    DXGI_ERROR_NOT_FOUND,
    DXGI_ERROR_SESSION_DISCONNECTED,
    DXGI_ERROR_UNSUPPORTED,
)

# Win32 HRESULT values from Microsoft Desktop Duplication sample.
E_ACCESSDENIED = 0x80070005
WAIT_ABANDONED_HRESULT = 0x00000080


class DXGITransientContext(str, Enum):
    """Error-category buckets matching Microsoft's desktop duplication sample."""

    SYSTEM_TRANSITION = "system_transition"
    CREATE_DUPLICATION = "create_duplication"
    FRAME_INFO = "frame_info"
    ENUM_OUTPUTS = "enum_outputs"


def hresult_u32(value: int) -> int:
    return ctypes.c_uint32(value).value


def com_error_hresult_u32(error: comtypes.COMError) -> int:
    hresult = getattr(error, "hresult", None)
    if hresult is None and len(error.args) > 0:
        hresult = error.args[0]
    if hresult is None:
        return 0
    return hresult_u32(int(hresult))


_TRANSIENT_HRESULTS: dict[DXGITransientContext, set[int]] = {
    # DesktopDuplication sample: SystemTransitionsExpectedErrors.
    DXGITransientContext.SYSTEM_TRANSITION: {
        hresult_u32(DXGI_ERROR_DEVICE_REMOVED),
        hresult_u32(DXGI_ERROR_ACCESS_LOST),
        hresult_u32(WAIT_ABANDONED_HRESULT),
    },
    # DesktopDuplication sample: CreateDuplicationExpectedErrors.
    DXGITransientContext.CREATE_DUPLICATION: {
        hresult_u32(DXGI_ERROR_DEVICE_REMOVED),
        hresult_u32(E_ACCESSDENIED),
        hresult_u32(DXGI_ERROR_UNSUPPORTED),
        hresult_u32(DXGI_ERROR_SESSION_DISCONNECTED),
    },
    # DesktopDuplication sample: FrameInfoExpectedErrors.
    DXGITransientContext.FRAME_INFO: {
        hresult_u32(DXGI_ERROR_DEVICE_REMOVED),
        hresult_u32(DXGI_ERROR_ACCESS_LOST),
    },
    # DesktopDuplication sample: EnumOutputsExpectedErrors.
    DXGITransientContext.ENUM_OUTPUTS: {
        hresult_u32(DXGI_ERROR_NOT_FOUND),
    },
}

# DXcam extension: this can occur when sessions reconnect/disconnect.
_TRANSIENT_HRESULTS[DXGITransientContext.FRAME_INFO].add(
    hresult_u32(DXGI_ERROR_SESSION_DISCONNECTED)
)


def is_transient_hresult(
    hresult_value: int,
    *contexts: DXGITransientContext,
) -> bool:
    hresult_value = hresult_u32(hresult_value)
    for context in contexts:
        if hresult_value in _TRANSIENT_HRESULTS[context]:
            return True
    return False


def is_transient_com_error(
    error: comtypes.COMError,
    *contexts: DXGITransientContext,
) -> bool:
    return is_transient_hresult(com_error_hresult_u32(error), *contexts)


def os_error_hresult_u32(error: OSError) -> int | None:
    hresult = getattr(error, "winerror", None)
    if hresult is None and len(error.args) > 0 and isinstance(error.args[0], int):
        hresult = error.args[0]
    if hresult is None:
        return None
    return hresult_u32(int(hresult))


def is_transient_os_error(
    error: OSError,
    *contexts: DXGITransientContext,
) -> bool:
    hresult = os_error_hresult_u32(error)
    if hresult is None:
        return False
    return is_transient_hresult(hresult, *contexts)


class ProgressiveWait:
    """Progressive wait helper mirroring Microsoft's DYNAMIC_WAIT behavior."""

    # Microsoft sample uses 2s to decide whether waits belong to one sequence.
    _WAIT_SEQUENCE_SECONDS = 2.0
    # Bands from DesktopDuplication sample: {250,20}, {2000,60}, {5000,stop}.
    _WAIT_BANDS: tuple[tuple[float, int | None], ...] = (
        (0.25, 20),
        (2.0, 60),
        (5.0, None),
    )

    def __init__(self) -> None:
        self._current_band_idx = 0
        self._wait_count_in_band = 0
        self._last_wakeup_time = 0.0

    def reset(self) -> None:
        self._current_band_idx = 0
        self._wait_count_in_band = 0
        self._last_wakeup_time = 0.0

    def next_delay_seconds(self) -> float:
        now = time.perf_counter()
        if self._last_wakeup_time > 0.0 and (
            now <= self._last_wakeup_time + self._WAIT_SEQUENCE_SECONDS
        ):
            wait_count_limit = self._WAIT_BANDS[self._current_band_idx][1]
            if (
                wait_count_limit is not None
                and self._wait_count_in_band > wait_count_limit
                and self._current_band_idx < len(self._WAIT_BANDS) - 1
            ):
                self._current_band_idx += 1
                self._wait_count_in_band = 0
        else:
            self._wait_count_in_band = 0
            self._current_band_idx = 0

        delay = self._WAIT_BANDS[self._current_band_idx][0]
        self._last_wakeup_time = now + delay
        self._wait_count_in_band += 1
        return delay
