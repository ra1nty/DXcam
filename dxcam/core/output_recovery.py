from __future__ import annotations

import ctypes
from dataclasses import dataclass
from typing import Any

import comtypes

from dxcam._libs.dxgi import DXGI_OUTPUT_DESC
from dxcam.core.com_ptr import release_com_pointer
from dxcam.core.device import Device
from dxcam.core.dxgi_errors import DXGITransientContext, is_transient_com_error
from dxcam.core.output import Output
from dxcam.types import Region


@dataclass(frozen=True)
class OutputState:
    width: int
    height: int
    rotation_angle: int
    region: Region
    region_was_clamped: bool


def _monitor_handle_to_int(handle: Any) -> int:
    value = getattr(handle, "value", handle)
    return int(value or 0)


def _region_in_bounds(region: Region, width: int, height: int) -> bool:
    left, top, right, bottom = region
    return width >= right > left >= 0 and height >= bottom > top >= 0


def _clamp_region(region: Region, width: int, height: int) -> Region:
    if width <= 0 or height <= 0:
        raise RuntimeError(
            f"Cannot clamp region with invalid output size {width}x{height}."
        )
    left, top, right, bottom = region
    left = min(max(int(left), 0), width - 1)
    top = min(max(int(top), 0), height - 1)
    right = min(max(int(right), left + 1), width)
    bottom = min(max(int(bottom), top + 1), height)
    return left, top, right, bottom


class OutputRecoveryHandler:
    """Resolves current output geometry/rotation during display transitions."""

    def __init__(self, output: Output, device: Device) -> None:
        self._output = output
        self._device = device

    def _refresh_output_desc(self) -> None:
        try:
            self._output.update_desc()
            return
        except comtypes.COMError as exc:
            if not is_transient_com_error(
                exc,
                DXGITransientContext.SYSTEM_TRANSITION,
                DXGITransientContext.ENUM_OUTPUTS,
            ):
                raise

        previous_monitor = _monitor_handle_to_int(self._output.hmonitor)
        previous_name = self._output.devicename

        fallback_output = None
        selected_output = None

        output_ptrs = self._device.enum_outputs()
        try:
            for output_ptr in output_ptrs:
                desc = DXGI_OUTPUT_DESC()
                try:
                    output_ptr.GetDesc(ctypes.byref(desc))
                except comtypes.COMError as exc:
                    if is_transient_com_error(
                        exc,
                        DXGITransientContext.SYSTEM_TRANSITION,
                        DXGITransientContext.ENUM_OUTPUTS,
                    ):
                        continue
                    raise

                if fallback_output is None:
                    fallback_output = output_ptr

                monitor = _monitor_handle_to_int(desc.Monitor)
                device_name = str(desc.DeviceName)
                if previous_monitor != 0 and monitor == previous_monitor:
                    selected_output = output_ptr
                    break
                if previous_name and device_name == previous_name:
                    selected_output = output_ptr
        finally:
            for output_ptr in output_ptrs:
                if output_ptr is selected_output:
                    continue
                release_com_pointer(output_ptr)

        if selected_output is None:
            selected_output = fallback_output
        if selected_output is None:
            raise RuntimeError("No DXGI outputs available during recovery.")

        previous_output = self._output.output
        self._output.output = selected_output
        if previous_output is not selected_output:
            release_com_pointer(previous_output)
        self._output.update_desc()

    def handle(
        self,
        *,
        requested_region: Region,
        region_set_by_user: bool,
    ) -> OutputState:
        self._refresh_output_desc()
        if not self._output.attached_to_desktop:
            raise RuntimeError("Output is not attached to desktop.")

        width, height = self._output.resolution
        rotation_angle = self._output.rotation_angle

        if not region_set_by_user:
            return OutputState(
                width=width,
                height=height,
                rotation_angle=rotation_angle,
                region=(0, 0, width, height),
                region_was_clamped=False,
            )

        if _region_in_bounds(requested_region, width, height):
            return OutputState(
                width=width,
                height=height,
                rotation_angle=rotation_angle,
                region=requested_region,
                region_was_clamped=False,
            )

        clamped = _clamp_region(requested_region, width, height)
        return OutputState(
            width=width,
            height=height,
            rotation_angle=rotation_angle,
            region=clamped,
            region_was_clamped=clamped != requested_region,
        )
