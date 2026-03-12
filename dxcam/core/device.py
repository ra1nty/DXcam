from __future__ import annotations

import ctypes
from dataclasses import dataclass
from typing import Any, cast

import comtypes
from dxcam._libs.d3d11 import (
    D3D_FEATURE_LEVEL_10_0,
    D3D_FEATURE_LEVEL_10_1,
    D3D_FEATURE_LEVEL_11_0,
    ID3D11Device,
    ID3D11DeviceContext,
)
from dxcam._libs.dxgi import (
    DXGI_ADAPTER_DESC1,
    IDXGIOutput1,
)
from dxcam.core.dxgi_errors import (
    DXGITransientContext,
    com_error_hresult_u32,
    is_transient_hresult,
)


@dataclass
class Device:
    """Direct3D11 device wrapper for one DXGI adapter."""

    adapter: Any
    device: Any = None
    context: Any = None
    im_context: Any = None
    desc: DXGI_ADAPTER_DESC1 | None = None

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
        device = cast(Any, self.device)
        device.GetImmediateContext(ctypes.byref(self.im_context))

    def enum_outputs(self) -> list[Any]:
        i = 0
        p_outputs: list[Any] = []
        while True:
            try:
                p_output = ctypes.POINTER(IDXGIOutput1)()
                self.adapter.EnumOutputs(i, ctypes.byref(p_output))
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

    @property
    def description(self) -> str:
        assert self.desc is not None
        return self.desc.Description

    @property
    def vram_size(self) -> int:
        assert self.desc is not None
        return self.desc.DedicatedVideoMemory

    @property
    def vendor_id(self) -> int:
        assert self.desc is not None
        return self.desc.VendorId

    def __repr__(self) -> str:
        assert self.desc is not None
        return "<{} Name:{} Dedicated VRAM:{}Mb VendorId:{}>".format(
            self.__class__.__name__,
            self.desc.Description,
            self.desc.DedicatedVideoMemory // 1048576,
            self.desc.VendorId,
        )
