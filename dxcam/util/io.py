import ctypes
from typing import List
from collections import defaultdict
import comtypes
from dxcam._libs.dxgi import (
    IDXGIFactory1,
    IDXGIAdapter1,
    IDXGIOutput1,
    DXGI_ERROR_NOT_FOUND,
)
from dxcam._libs.user32 import (
    DISPLAY_DEVICE,
    MONITORINFOEXW,
    DISPLAY_DEVICE_ACTIVE,
    DISPLAY_DEVICE_PRIMARY_DEVICE,
)


def enum_dxgi_adapters() -> List[ctypes.POINTER(IDXGIAdapter1)]:
    create_dxgi_factory = ctypes.windll.dxgi.CreateDXGIFactory1
    create_dxgi_factory.argtypes = (comtypes.GUID, ctypes.POINTER(ctypes.c_void_p))
    create_dxgi_factory.restype = ctypes.c_int32
    pfactory = ctypes.c_void_p(0)
    create_dxgi_factory(IDXGIFactory1._iid_, ctypes.byref(pfactory))
    dxgi_factory = ctypes.POINTER(IDXGIFactory1)(pfactory.value)
    i = 0
    p_adapters = list()
    while True:
        try:
            p_adapter = ctypes.POINTER(IDXGIAdapter1)()
            dxgi_factory.EnumAdapters1(i, ctypes.byref(p_adapter))
            p_adapters.append(p_adapter)
            i += 1
        except comtypes.COMError as ce:
            if ctypes.c_int32(DXGI_ERROR_NOT_FOUND).value == ce.args[0]:
                break
            else:
                raise ce
    return p_adapters


def enum_dxgi_outputs(
    dxgi_adapter: ctypes.POINTER(IDXGIAdapter1),
) -> List[ctypes.POINTER(IDXGIOutput1)]:
    i = 0
    p_outputs = list()
    while True:
        try:
            p_output = ctypes.POINTER(IDXGIOutput1)()
            dxgi_adapter.EnumOutputs(i, ctypes.byref(p_output))
            p_outputs.append(p_output)
            i += 1
        except comtypes.COMError as ce:
            if ctypes.c_int32(DXGI_ERROR_NOT_FOUND).value == ce.args[0]:
                break
            else:
                raise ce
    return p_outputs


def get_output_metadata():
    mapping_adapter = defaultdict(list)
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
    return mapping_adapter


def get_monitor_name_by_handle(hmonitor):
    info = MONITORINFOEXW()
    info.cbSize = ctypes.sizeof(MONITORINFOEXW)
    if ctypes.windll.user32.GetMonitorInfoW(hmonitor, ctypes.byref(info)):
        return info
    return None
