from __future__ import annotations

import ctypes
from dataclasses import InitVar, dataclass, field
from typing import Any, cast

from dxcam._libs.d3d11 import (
    D3D11_CPU_ACCESS_READ,
    D3D11_TEXTURE2D_DESC,
    D3D11_USAGE_STAGING,
    DXGI_FORMAT_B8G8R8A8_UNORM,
    ID3D11Texture2D,
)
from dxcam._libs.dxgi import DXGI_MAPPED_RECT, IDXGISurface
from dxcam.core.device import Device
from dxcam.core.output import Output


@dataclass
class StageSurface:
    """CPU-readable staging texture used to map duplicated frames."""

    width: int = 0
    height: int = 0
    dxgi_format: int = DXGI_FORMAT_B8G8R8A8_UNORM
    desc: D3D11_TEXTURE2D_DESC = field(default_factory=D3D11_TEXTURE2D_DESC)
    texture: Any = None
    interface: Any = None
    output: InitVar[Output | None] = None
    device: InitVar[Device | None] = None

    def __post_init__(self, output: Output | None, device: Device | None) -> None:
        if output is None or device is None:
            raise ValueError("StageSurface requires valid output and device instances.")
        self.rebuild(output, device)

    def release(self) -> None:
        if self.texture is not None:
            self.width = 0
            self.height = 0
            self.texture.Release()
            self.texture = None
            self.interface = None

    def rebuild(
        self,
        output: Output,
        device: Device,
        dim: tuple[int, int] | None = None,
    ) -> None:
        if dim is not None:
            self.width, self.height = dim
        else:
            self.width, self.height = output.surface_size
        if self.texture is None:
            self.desc.Width = self.width
            self.desc.Height = self.height
            self.desc.Format = self.dxgi_format
            self.desc.MipLevels = 1
            self.desc.ArraySize = 1
            self.desc.SampleDesc.Count = 1
            self.desc.SampleDesc.Quality = 0
            self.desc.Usage = D3D11_USAGE_STAGING
            self.desc.CPUAccessFlags = D3D11_CPU_ACCESS_READ
            self.desc.MiscFlags = 0
            self.desc.BindFlags = 0
            self.texture = ctypes.POINTER(ID3D11Texture2D)()
            device.device.CreateTexture2D(
                ctypes.byref(self.desc),
                None,
                ctypes.byref(self.texture),
            )
            texture = cast(Any, self.texture)
            self.interface = texture.QueryInterface(IDXGISurface)

    def map(self) -> DXGI_MAPPED_RECT:
        if self.interface is None:
            raise RuntimeError("StageSurface interface is not initialized.")
        rect: DXGI_MAPPED_RECT = DXGI_MAPPED_RECT()
        self.interface.Map(ctypes.byref(rect), 1)
        return rect

    def unmap(self) -> None:
        if self.interface is None:
            raise RuntimeError("StageSurface interface is not initialized.")
        self.interface.Unmap()

    def __repr__(self) -> str:
        return "<{} Initialized:{} Size:{} Format:{}>".format(
            self.__class__.__name__,
            self.texture is not None,
            (self.width, self.height),
            "DXGI_FORMAT_B8G8R8A8_UNORM",
        )
