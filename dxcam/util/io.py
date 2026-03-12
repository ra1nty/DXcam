from __future__ import annotations

import ctypes
from collections import defaultdict
from typing import Any, cast

import comtypes
from dxcam._libs.dxgi import (
    IDXGIAdapter1,
    IDXGIFactory1,
    IDXGIOutput1,
)
from dxcam.core.dxgi_errors import (
    DXGITransientContext,
    com_error_hresult_u32,
    is_transient_hresult,
)
from dxcam._libs.user32 import (
    DISPLAY_DEVICE,
    DISPLAY_DEVICE_ACTIVE,
    DISPLAY_DEVICE_PRIMARY_DEVICE,
    MONITORINFOEXW,
)


def enum_dxgi_adapters() -> list[Any]:
    create_dxgi_factory = ctypes.windll.dxgi.CreateDXGIFactory1
    create_dxgi_factory.argtypes = (comtypes.GUID, ctypes.POINTER(ctypes.c_void_p))
    create_dxgi_factory.restype = ctypes.c_int32
    pfactory = ctypes.c_void_p(0)
    create_dxgi_factory(IDXGIFactory1._iid_, ctypes.byref(pfactory))
    dxgi_factory = ctypes.cast(pfactory, ctypes.POINTER(IDXGIFactory1))
    factory = cast(Any, dxgi_factory)
    i = 0
    p_adapters: list[Any] = []
    while True:
        try:
            p_adapter = ctypes.POINTER(IDXGIAdapter1)()
            factory.EnumAdapters1(i, ctypes.byref(p_adapter))
            p_adapters.append(p_adapter)
            i += 1
        except comtypes.COMError as ce:
            hresult_u32 = com_error_hresult_u32(ce)
            if is_transient_hresult(
                hresult_u32,
                DXGITransientContext.ENUM_OUTPUTS,
            ):
                break
            raise
    return p_adapters


def enum_dxgi_outputs(
    dxgi_adapter: Any,
) -> list[Any]:
    i = 0
    p_outputs: list[Any] = []
    while True:
        try:
            p_output = ctypes.POINTER(IDXGIOutput1)()
            dxgi_adapter.EnumOutputs(i, ctypes.byref(p_output))
            p_outputs.append(p_output)
            i += 1
        except comtypes.COMError as ce:
            hresult_u32 = com_error_hresult_u32(ce)
            if is_transient_hresult(
                hresult_u32,
                DXGITransientContext.ENUM_OUTPUTS,
            ):
                break
            raise
    return p_outputs


def get_output_metadata() -> dict[str, list[Any]]:
    mapping_adapter: dict[str, list[Any]] = defaultdict(list)
    adapter = DISPLAY_DEVICE()
    adapter.cb = ctypes.sizeof(adapter)
    i = 0
    # Enumerate all adapters
    while ctypes.windll.user32.EnumDisplayDevicesW(0, i, ctypes.byref(adapter), 1):
        if adapter.StateFlags & DISPLAY_DEVICE_ACTIVE != 0:
            is_primary = bool(adapter.StateFlags & DISPLAY_DEVICE_PRIMARY_DEVICE)
            mapping_adapter[adapter.DeviceName] = [adapter.DeviceString, is_primary, []]
            display = DISPLAY_DEVICE()
            display.cb = ctypes.sizeof(adapter)
            j = 0
            # Enumerate Monitors
            while ctypes.windll.user32.EnumDisplayDevicesW(
                adapter.DeviceName, j, ctypes.byref(display), 0
            ):
                mapping_adapter[adapter.DeviceName][2].append(
                    (
                        display.DeviceName,
                        display.DeviceString,
                    )
                )
                j += 1
        i += 1
    return dict(mapping_adapter)


def get_monitor_name_by_handle(hmonitor: Any) -> MONITORINFOEXW | None:
    info = MONITORINFOEXW()
    info.cbSize = ctypes.sizeof(MONITORINFOEXW)
    if ctypes.windll.user32.GetMonitorInfoW(hmonitor, ctypes.byref(info)):
        return info
    return None
