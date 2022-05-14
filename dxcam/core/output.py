import ctypes
from typing import Tuple
from dataclasses import dataclass
from dxcam._libs.d3d11 import *
from dxcam._libs.dxgi import *


@dataclass
class Output:
    output: ctypes.POINTER(IDXGIOutput1)
    rotation_mapping: tuple = (0, 0, 90, 180, 270)
    desc: DXGI_OUTPUT_DESC = None

    def __post_init__(self):
        self.desc = DXGI_OUTPUT_DESC()
        self.update_desc()

    def update_desc(self):
        if self.desc is None:
            self.desc = DXGI_OUTPUT_DESC()
        self.output.GetDesc(ctypes.byref(self.desc))

    @property
    def hmonitor(self) -> wintypes.HMONITOR:
        return self.desc.Monitor

    @property
    def devicename(self) -> str:
        return self.desc.DeviceName

    @property
    def resolution(self) -> Tuple[int, int]:
        return (
            (self.desc.DesktopCoordinates.right - self.desc.DesktopCoordinates.left),
            (self.desc.DesktopCoordinates.bottom - self.desc.DesktopCoordinates.top),
        )

    @property
    def attached_to_desktop(self) -> bool:
        return bool(self.desc.AttachedToDesktop)

    @property
    def rotation_angle(self) -> int:
        return self.rotation_mapping[self.desc.Rotation]

    def __repr__(self) -> str:
        return "<{} Name:{} Resolution:{} Rotation:{}>".format(
            self.__class__.__name__,
            self.devicename,
            self.resolution,
            self.rotation_angle,
        )
