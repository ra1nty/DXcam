from __future__ import annotations

import ctypes
import logging
import os
from contextlib import contextmanager
from dataclasses import InitVar, dataclass, field
from typing import Any, Iterator, cast

import comtypes

from dxcam._libs.d3d11 import DXGI_FORMAT_B8G8R8A8_UNORM, ID3D11Texture2D
from dxcam._libs.dxgi import (
    DXGI_OUTDUPL_FLAG_NONE,
    DXGI_ERROR_WAIT_TIMEOUT,
    DXGI_OUTDUPL_FRAME_INFO,
    IDXGIOutputDuplication,
    IDXGIOutput5,
    IDXGIResource,
)
from dxcam.core.com_ptr import release_com_pointer
from dxcam.core.device import Device
from dxcam.core.dxgi_errors import (
    DXGITransientContext,
    com_error_hresult_u32,
    hresult_u32,
    is_transient_hresult,
)
from dxcam.core.output import Output

logger = logging.getLogger(__name__)
ENABLE_DUPLICATE_OUTPUT1 = os.getenv("DXCAM_USE_DUPLICATE_OUTPUT1", "1").lower() in {
    "1",
    "true",
    "yes",
}
DXGI_ERROR_WAIT_TIMEOUT_U32 = hresult_u32(DXGI_ERROR_WAIT_TIMEOUT)


@dataclass
class DXGIDuplicator:
    """Desktop Duplication API wrapper for acquiring frame textures."""

    texture: Any = field(default_factory=lambda: ctypes.POINTER(ID3D11Texture2D)())
    duplicator: Any = None
    updated: bool = False
    output: InitVar[Output | None] = None
    device: InitVar[Device | None] = None
    latest_frame_ticks: int = 0
    accumulated_frames: int = 0
    # ticks per second of the system
    performance_frequency: int = 0
    _frame_held: bool = False

    def _drop_texture_reference(self) -> None:
        release_com_pointer(self.texture)
        self.texture = ctypes.POINTER(ID3D11Texture2D)()

    def __post_init__(self, output: Output | None, device: Device | None) -> None:
        if output is None or device is None:
            raise ValueError(
                "DXGIDuplicator requires valid output and device instances."
            )
        self.duplicator = ctypes.POINTER(IDXGIOutputDuplication)()
        self._duplicate_output(output=output, device=device)
        freq = ctypes.c_longlong()
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.QueryPerformanceFrequency(ctypes.byref(freq))
        self.performance_frequency = freq.value

    def _duplicate_output(self, output: Output, device: Device) -> None:
        output_ptr = output.output
        if ENABLE_DUPLICATE_OUTPUT1 and self._try_duplicate_output1(
            output_ptr=output_ptr, device=device
        ):
            return
        if not ENABLE_DUPLICATE_OUTPUT1:
            logger.debug(
                "Using legacy IDXGIOutput1.DuplicateOutput. "
                "Set DXCAM_USE_DUPLICATE_OUTPUT1=1 to enable DuplicateOutput1."
            )
        try:
            output_ptr.DuplicateOutput(device.device, ctypes.byref(self.duplicator))
        except comtypes.COMError as ce:
            err = com_error_hresult_u32(ce)
            if is_transient_hresult(
                err,
                DXGITransientContext.CREATE_DUPLICATION,
                DXGITransientContext.SYSTEM_TRANSITION,
            ):
                logger.info(
                    "DuplicateOutput transient failure (HRESULT=0x%08X).",
                    err,
                )
            raise

    def _try_duplicate_output1(self, output_ptr: Any, device: Device) -> bool:
        try:
            output5 = output_ptr.QueryInterface(IDXGIOutput5)
        except comtypes.COMError:
            return False

        supported_formats = (ctypes.c_uint * 1)(DXGI_FORMAT_B8G8R8A8_UNORM)
        try:
            output5.DuplicateOutput1(
                ctypes.cast(device.device, ctypes.c_void_p),
                DXGI_OUTDUPL_FLAG_NONE,
                len(supported_formats),
                supported_formats,
                ctypes.byref(self.duplicator),
            )
            logger.debug("Using IDXGIOutput5.DuplicateOutput1 for capture.")
            return True
        except comtypes.COMError as ce:
            err = com_error_hresult_u32(ce)
            if is_transient_hresult(
                err,
                DXGITransientContext.CREATE_DUPLICATION,
                DXGITransientContext.SYSTEM_TRANSITION,
            ):
                logger.info(
                    "DuplicateOutput1 transient failure (HRESULT=0x%08X); "
                    "falling back to DuplicateOutput.",
                    err,
                )
            else:
                logger.debug(
                    "DuplicateOutput1 failed; falling back to DuplicateOutput.",
                    exc_info=True,
                )
            self.duplicator = ctypes.POINTER(IDXGIOutputDuplication)()
            return False

    def _update_frame(self, wait_for_frame: bool = False) -> bool:
        del wait_for_frame  # DXGI path does not use extra wait logic.
        if self._frame_held:
            if not self._release_frame():
                self.updated = False
                return False

        info = DXGI_OUTDUPL_FRAME_INFO()
        res = ctypes.POINTER(IDXGIResource)()
        try:
            try:
                self.duplicator.AcquireNextFrame(
                    0,
                    ctypes.byref(info),
                    ctypes.byref(res),
                )
            except comtypes.COMError as ce:
                hresult = com_error_hresult_u32(ce)
                if hresult == DXGI_ERROR_WAIT_TIMEOUT_U32:
                    self.updated = False
                    self.accumulated_frames = 0
                    return True
                if is_transient_hresult(
                    hresult,
                    DXGITransientContext.FRAME_INFO,
                    DXGITransientContext.SYSTEM_TRANSITION,
                ):
                    logger.warning(
                        "Desktop duplication access loss/system transition detected "
                        "(HRESULT=0x%08X). Triggering output-change recovery.",
                        hresult,
                    )
                    self.updated = False
                    self._frame_held = False
                    return False
                raise
            self._frame_held = True
            try:
                resource = cast(Any, res)
                self._drop_texture_reference()
                self.texture = resource.QueryInterface(ID3D11Texture2D)
            except comtypes.COMError:
                self._release_frame()
                self.texture = ctypes.POINTER(ID3D11Texture2D)()
                self.updated = False
                return True
            present_ticks = int(info.LastPresentTime)
            self.accumulated_frames = int(info.AccumulatedFrames)
            if present_ticks > 0:
                self.latest_frame_ticks = present_ticks
            else:
                mouse_ticks = int(info.LastMouseUpdateTime)
                if mouse_ticks > 0:
                    self.latest_frame_ticks = mouse_ticks
            self.updated = True
            return True
        finally:
            release_com_pointer(res)

    @contextmanager
    def acquire_frame(
        self, wait_for_frame: bool = False
    ) -> Iterator[tuple[bool, bool, int]]:
        ok = self._update_frame(wait_for_frame=wait_for_frame)
        updated = ok and self.updated
        frame_ticks = self.latest_frame_ticks
        try:
            yield ok, updated, frame_ticks
        finally:
            if ok and updated:
                self._finish_frame()

    @property
    def latest_frame_time(self) -> float:
        return self.ticks_to_seconds(self.latest_frame_ticks)

    def ticks_to_seconds(self, ticks: int) -> float:
        if self.performance_frequency <= 0:
            return 0.0
        return ticks / self.performance_frequency

    def _release_frame(self) -> bool:
        """Per Microsoft Doc
        https://learn.microsoft.com/en-us/windows/win32/api/dxgi1_2/nf-dxgi1_2-idxgioutputduplication-releaseframe#remarks
        This should be called just before AquireNextFrame, but we found audio artifacts
        and frame pacing issue (need longer timeout for AquireNextFrame to compensate)

        So DXCam default to early release.

        Returns:
            bool: Sucessfully
        """
        if self.duplicator is None or not self._frame_held:
            return True
        self._drop_texture_reference()
        try:
            self.duplicator.ReleaseFrame()
        except comtypes.COMError as ce:
            hresult = com_error_hresult_u32(ce)
            self._frame_held = False
            if is_transient_hresult(
                hresult,
                DXGITransientContext.FRAME_INFO,
                DXGITransientContext.SYSTEM_TRANSITION,
            ):
                logger.warning(
                    "ReleaseFrame access loss/system transition detected "
                    "(HRESULT=0x%08X).",
                    hresult,
                )
                return False
            raise
        self._frame_held = False
        return True

    def _finish_frame(self) -> bool:
        # DXGI path releases immediately after staging copy for better pacing.
        return self._release_frame()

    def release(self) -> None:
        self._drop_texture_reference()
        if self.duplicator is not None:
            self._release_frame()
            try:
                release_com_pointer(self.duplicator)
            except comtypes.COMError:
                logger.debug(
                    "Ignoring COMError while releasing duplicator.", exc_info=True
                )
            self.duplicator = None

    def __repr__(self) -> str:
        return "<{} Initalized:{}>".format(
            self.__class__.__name__,
            self.duplicator is not None,
        )
