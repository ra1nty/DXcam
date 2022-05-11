from .libs.d3d11 import *
from .libs.dxgi import *
from .libs.user32 import *


def enum_dxgi_adapters() -> list[ctypes.POINTER(IDXGIAdapter1)]:
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
) -> list[ctypes.POINTER(IDXGIOutput1)]:
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


def get_output_metadata_mapping():
    DISPLAY_DEVICE_ACTIVE = 1
    DISPLAY_DEVICE_PRIMARY_DEVICE = 4
    mapping = dict()

    display_device = DISPLAY_DEVICE()
    display_device.cb = ctypes.sizeof(display_device)
    i = 0
    while ctypes.windll.user32.EnumDisplayDevicesW(
        0, i, ctypes.byref(display_device), 0
    ):
        device_name: str = display_device.DeviceName
        if display_device.StateFlags & DISPLAY_DEVICE_ACTIVE != 0:
            j = 0
            is_primary = bool(display_device.StateFlags & DISPLAY_DEVICE_PRIMARY_DEVICE)
            while ctypes.windll.user32.EnumDisplayDevicesW(
                device_name, j, ctypes.byref(display_device), 0
            ):
                mapping[device_name] = (
                    display_device.DeviceString,
                    is_primary,
                )
                j += 1
        i += 1

    return mapping
