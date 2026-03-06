from __future__ import annotations

import ctypes
from dataclasses import InitVar, dataclass, field
from typing import Any, cast

import comtypes

from dxcam._libs.d3d11 import ID3D11Texture2D
from dxcam._libs.dxgi import (
    DXGI_ERROR_ACCESS_LOST,
    DXGI_ERROR_WAIT_TIMEOUT,
    DXGI_OUTDUPL_FRAME_INFO,
    IDXGIOutputDuplication,
    IDXGIResource,
)
from dxcam.core.device import Device
from dxcam.core.output import Output


@dataclass
class Duplicator:
    """Desktop Duplication API wrapper for acquiring frame textures."""

    texture: Any = field(default_factory=lambda: ctypes.POINTER(ID3D11Texture2D)())
    duplicator: Any = None
    updated: bool = False
    output: InitVar[Output | None] = None
    device: InitVar[Device | None] = None
    latest_frame_time: float = 0.0
    # ticks per second of the system
    performance_frequency: int = 0

    def __post_init__(self, output: Output | None, device: Device | None) -> None:
        if output is None or device is None:
            raise ValueError("Duplicator requires valid output and device instances.")
        self.duplicator = ctypes.POINTER(IDXGIOutputDuplication)()
        output.output.DuplicateOutput(device.device, ctypes.byref(self.duplicator))
        freq = ctypes.c_longlong()
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.QueryPerformanceFrequency(ctypes.byref(freq))
        self.performance_frequency = freq.value

    def update_frame(self) -> bool:
        info = DXGI_OUTDUPL_FRAME_INFO()
        res: Any = ctypes.POINTER(IDXGIResource)()
        try:
            self.duplicator.AcquireNextFrame(
                0,
                ctypes.byref(info),
                ctypes.byref(res),
            )
        except comtypes.COMError as ce:
            if ctypes.c_int32(DXGI_ERROR_ACCESS_LOST).value == ce.args[0]:
                return False
            if ctypes.c_int32(DXGI_ERROR_WAIT_TIMEOUT).value == ce.args[0]:
                self.updated = False
                return True
            raise
        try:
            resource = cast(Any, res)
            self.texture = resource.QueryInterface(ID3D11Texture2D)
        except comtypes.COMError:
            self.duplicator.ReleaseFrame()
            self.texture = ctypes.POINTER(ID3D11Texture2D)()
            self.updated = False
            return True
        self.latest_frame_time = info.LastPresentTime / self.performance_frequency
        self.updated = True
        return True

    def release_frame(self) -> None:
        self.duplicator.ReleaseFrame()

    def release(self) -> None:
        if self.duplicator is not None:
            self.duplicator.Release()
            self.duplicator = None

    def __repr__(self) -> str:
        return "<{} Initalized:{}>".format(
            self.__class__.__name__,
            self.duplicator is not None,
        )
