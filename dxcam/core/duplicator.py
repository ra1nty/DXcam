import ctypes
from dataclasses import dataclass, InitVar
from dxcam._libs.d3d11 import *
from dxcam._libs.dxgi import *
from dxcam.core.device import Device
from dxcam.core.output import Output


@dataclass
class Duplicator:
    texture: ctypes.POINTER(ID3D11Texture2D) = ctypes.POINTER(ID3D11Texture2D)()
    duplicator: ctypes.POINTER(IDXGIOutputDuplication) = None
    updated: bool = False
    output: InitVar[Output] = None
    device: InitVar[Device] = None
    latest_frame_time: float = 0.0
    # ticks per second of the system
    performance_frequency: int = 0

    def __post_init__(self, output: Output, device: Device) -> None:
        self.duplicator = ctypes.POINTER(IDXGIOutputDuplication)()
        output.output.DuplicateOutput(device.device, ctypes.byref(self.duplicator))
        freq = ctypes.c_longlong()
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.QueryPerformanceFrequency(ctypes.byref(freq))
        self.performance_frequency = freq.value

    def update_frame(self):
        info = DXGI_OUTDUPL_FRAME_INFO()
        res = ctypes.POINTER(IDXGIResource)()
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
            else:
                raise ce
        try:
            self.texture = res.QueryInterface(ID3D11Texture2D)
        except comtypes.COMError as ce:
            self.duplicator.ReleaseFrame()
        self.latest_frame_time = info.LastPresentTime / self.performance_frequency
        self.updated = True
        return True

    def release_frame(self):
        self.duplicator.ReleaseFrame()

    def release(self):
        if self.duplicator is not None:
            self.duplicator.Release()
            self.duplicator = None

    def __repr__(self) -> str:
        return "<{} Initalized:{}>".format(
            self.__class__.__name__,
            self.duplicator is not None,
        )
