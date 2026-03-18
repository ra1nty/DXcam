from __future__ import annotations

import ctypes
from contextlib import contextmanager
from dataclasses import InitVar, dataclass, field
from typing import Any, Iterator, cast

from dxcam._libs.d3d11 import (
    D3D11_BOX,
    D3D11_CPU_ACCESS_READ,
    D3D11_TEXTURE2D_DESC,
    D3D11_USAGE_STAGING,
    DXGI_FORMAT_B8G8R8A8_UNORM,
    ID3D11Texture2D,
)
from dxcam._libs.dxgi import DXGI_MAPPED_RECT, IDXGISurface
from dxcam.core.com_ptr import release_com_pointer
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
    _copy_region_box: D3D11_BOX = field(default_factory=D3D11_BOX, repr=False)
    output: InitVar[Output | None] = None
    device: InitVar[Device | None] = None
    dim: InitVar[tuple[int, int] | None] = None
    _output: Output | None = field(default=None, init=False, repr=False)
    _device: Device | None = field(default=None, init=False, repr=False)

    def __post_init__(
        self,
        output: Output | None,
        device: Device | None,
        dim: tuple[int, int] | None,
    ) -> None:
        if output is None or device is None:
            raise ValueError("StageSurface requires valid output and device instances.")
        self._output = output
        self._device = device
        self._copy_region_box.front = 0
        self._copy_region_box.back = 1
        self.rebuild(dim=dim)

    def release(self) -> None:
        if self.texture is not None or self.interface is not None:
            self.width = 0
            self.height = 0
            release_com_pointer(self.interface)
            release_com_pointer(self.texture)
            self.texture = None
            self.interface = None

    def rebuild(
        self,
        dim: tuple[int, int] | None = None,
    ) -> None:
        if self._output is None or self._device is None:
            raise RuntimeError("StageSurface context is not initialized.")
        if dim is not None:
            self.width, self.height = dim
        else:
            self.width, self.height = self._output.surface_size
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
            self._device.device.CreateTexture2D(
                ctypes.byref(self.desc),
                None,
                ctypes.byref(self.texture),
            )
            texture = cast(Any, self.texture)
            self.interface = texture.QueryInterface(IDXGISurface)

    def rebind(self, *, output: Output, device: Device) -> None:
        self._output = output
        self._device = device

    def ensure_size(
        self,
        *,
        dim: tuple[int, int],
    ) -> None:
        """Ensure this stage surface matches ``dim``."""
        width, height = dim
        if self.texture is None or self.width != width or self.height != height:
            self.release()
            self.rebuild(dim=dim)

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

    @contextmanager
    def mapped(self) -> Iterator[DXGI_MAPPED_RECT]:
        """Context-manager wrapper around map/unmap."""
        rect = self.map()
        try:
            yield rect
        finally:
            self.unmap()

    def copy_region_from(
        self,
        *,
        im_context: Any,
        src_texture: Any,
        src_region: tuple[int, int, int, int],
    ) -> None:
        """Copy a source texture region into this staging surface."""
        if self.texture is None:
            raise RuntimeError("StageSurface texture is not initialized.")
        self._copy_region_box.left = src_region[0]
        self._copy_region_box.top = src_region[1]
        self._copy_region_box.right = src_region[2]
        self._copy_region_box.bottom = src_region[3]
        im_context.CopySubresourceRegion(
            self.texture,
            0,
            0,
            0,
            0,
            src_texture,
            0,
            ctypes.byref(self._copy_region_box),
        )

    def __repr__(self) -> str:
        return "<{} Initialized:{} Size:{} Format:{}>".format(
            self.__class__.__name__,
            self.texture is not None,
            (self.width, self.height),
            "DXGI_FORMAT_B8G8R8A8_UNORM",
        )
