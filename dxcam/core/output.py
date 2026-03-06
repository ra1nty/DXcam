from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
from dataclasses import dataclass
from typing import Any

from dxcam._libs.dxgi import DXGI_OUTPUT_DESC


@dataclass
class Output:
    """DXGI output wrapper with geometry and rotation helpers."""

    output: Any
    rotation_mapping: tuple[int, int, int, int, int] = (0, 0, 90, 180, 270)
    desc: DXGI_OUTPUT_DESC | None = None

    def __post_init__(self) -> None:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        self.desc = DXGI_OUTPUT_DESC()
        self.update_desc()

    def update_desc(self) -> None:
        if self.desc is None:
            self.desc = DXGI_OUTPUT_DESC()
        self.output.GetDesc(ctypes.byref(self.desc))

    @property
    def hmonitor(self) -> wintypes.HMONITOR:
        assert self.desc is not None
        return self.desc.Monitor

    @property
    def devicename(self) -> str:
        assert self.desc is not None
        return self.desc.DeviceName

    @property
    def resolution(self) -> tuple[int, int]:
        assert self.desc is not None
        return (
            (self.desc.DesktopCoordinates.right - self.desc.DesktopCoordinates.left),
            (self.desc.DesktopCoordinates.bottom - self.desc.DesktopCoordinates.top),
        )

    @property
    def surface_size(self) -> tuple[int, int]:
        if self.rotation_angle in (90, 270):
            return self.resolution[1], self.resolution[0]
        return self.resolution

    @property
    def attached_to_desktop(self) -> bool:
        assert self.desc is not None
        return bool(self.desc.AttachedToDesktop)

    @property
    def rotation_angle(self) -> int:
        assert self.desc is not None
        return self.rotation_mapping[self.desc.Rotation]

    def __repr__(self) -> str:
        return "<{} Name:{} Resolution:{} Rotation:{}>".format(
            self.__class__.__name__,
            self.devicename,
            self.resolution,
            self.rotation_angle,
        )
