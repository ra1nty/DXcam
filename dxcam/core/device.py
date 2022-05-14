import ctypes
from dataclasses import dataclass
from typing import List
import comtypes
from dxcam._libs.d3d11 import *
from dxcam._libs.dxgi import *


@dataclass
class Device:
    adapter: ctypes.POINTER(IDXGIAdapter1)
    device: ctypes.POINTER(ID3D11Device) = None
    context: ctypes.POINTER(ID3D11DeviceContext) = None
    im_context: ctypes.POINTER(ID3D11DeviceContext) = None
    desc: DXGI_ADAPTER_DESC1 = None

    def __post_init__(self) -> None:
        self.desc = DXGI_ADAPTER_DESC1()
        self.adapter.GetDesc1(ctypes.byref(self.desc))

        D3D11CreateDevice = ctypes.windll.d3d11.D3D11CreateDevice

        feature_levels = [
            D3D_FEATURE_LEVEL_11_0,
            D3D_FEATURE_LEVEL_10_1,
            D3D_FEATURE_LEVEL_10_0,
        ]

        self.device = ctypes.POINTER(ID3D11Device)()
        self.context = ctypes.POINTER(ID3D11DeviceContext)()
        self.im_context = ctypes.POINTER(ID3D11DeviceContext)()

        D3D11CreateDevice(
            self.adapter,
            0,
            None,
            0,
            ctypes.byref((ctypes.c_uint * len(feature_levels))(*feature_levels)),
            len(feature_levels),
            7,
            ctypes.byref(self.device),
            None,
            ctypes.byref(self.context),
        )
        self.device.GetImmediateContext(ctypes.byref(self.im_context))

    def enum_outputs(self) -> List[ctypes.POINTER(IDXGIOutput1)]:
        i = 0
        p_outputs = []
        while True:
            try:
                p_output = ctypes.POINTER(IDXGIOutput1)()
                self.adapter.EnumOutputs(i, ctypes.byref(p_output))
                p_outputs.append(p_output)
                i += 1
            except comtypes.COMError as ce:
                if ctypes.c_int32(DXGI_ERROR_NOT_FOUND).value == ce.args[0]:
                    break
                else:
                    raise ce
        return p_outputs

    @property
    def description(self) -> str:
        return self.desc.Description

    @property
    def vram_size(self) -> int:
        return self.desc.DedicatedVideoMemory

    @property
    def vendor_id(self) -> int:
        return self.desc.VendorId

    def __repr__(self) -> str:
        return "<{} Name:{} Dedicated VRAM:{}Mb VendorId:{}>".format(
            self.__class__.__name__,
            self.desc.Description,
            self.desc.DedicatedVideoMemory // 1048576,
            self.desc.VendorId,
        )
