from __future__ import annotations

import logging
import time
from typing import Any, Callable

import comtypes

from dxcam.core.dxgi_errors import (
    DXGITransientContext,
    ProgressiveWait,
    com_error_hresult_u32,
    is_transient_com_error,
    is_transient_os_error,
    os_error_hresult_u32,
)
from dxcam.core.output_recovery import OutputRecoveryHandler, OutputState
from dxcam.types import CaptureBackend, Region

_TRANSIENT_RECOVERY_CONTEXTS = (
    DXGITransientContext.SYSTEM_TRANSITION,
    DXGITransientContext.CREATE_DUPLICATION,
    DXGITransientContext.FRAME_INFO,
    DXGITransientContext.ENUM_OUTPUTS,
)


class DisplayRecoveryHandler:
    """Recovers capture resources after output mode/geometry changes."""

    def __init__(
        self,
        *,
        backend: CaptureBackend,
        output_recovery: OutputRecoveryHandler,
        release_resources: Callable[[], None],
        rebuild_stage_surface: Callable[[], None],
        create_duplicator: Callable[[], Any],
        rebuild_frame_buffer: Callable[[Region], None],
        logger: logging.Logger | None = None,
    ) -> None:
        self._backend = backend
        self._output_recovery = output_recovery
        self._release_resources = release_resources
        self._rebuild_stage_surface = rebuild_stage_surface
        self._create_duplicator = create_duplicator
        self._rebuild_frame_buffer = rebuild_frame_buffer
        self._wait = ProgressiveWait()
        self._logger = logger or logging.getLogger(__name__)

    def _retry_hresult_u32(
        self,
        exc: Exception,
        *,
        is_dxgi_backend: bool,
    ) -> int | None:
        if isinstance(exc, comtypes.COMError):
            if is_dxgi_backend and not is_transient_com_error(
                exc,
                *_TRANSIENT_RECOVERY_CONTEXTS,
            ):
                raise
            return com_error_hresult_u32(exc)
        if isinstance(exc, OSError):
            if is_dxgi_backend and not is_transient_os_error(
                exc,
                *_TRANSIENT_RECOVERY_CONTEXTS,
            ):
                raise
            return os_error_hresult_u32(exc)
        if isinstance(exc, RuntimeError):
            return None
        raise exc

    def handle(
        self,
        *,
        region: Region,
        region_set_by_user: bool,
        is_capturing: bool,
    ) -> tuple[Any, OutputState]:
        is_dxgi_backend = self._backend == "dxgi"
        attempt = 0

        while True:
            attempt += 1
            try:
                # Keep each attempt isolated in case a previous attempt partially
                # rebuilt DXGI/D3D objects before failing.
                self._release_resources()
                output_state = self._output_recovery.handle(
                    requested_region=region,
                    region_set_by_user=region_set_by_user,
                )
                if output_state.region_was_clamped:
                    self._logger.warning(
                        "Requested region %s is out of bounds for new output size "
                        "%dx%d; clamping to %s.",
                        region,
                        output_state.width,
                        output_state.height,
                        output_state.region,
                    )
                if is_capturing:
                    self._rebuild_frame_buffer(output_state.region)
                self._rebuild_stage_surface()
                duplicator = self._create_duplicator()
                self._wait.reset()

                if attempt > 1:
                    self._logger.info(
                        "Output recovery succeeded after %d attempt(s).",
                        attempt,
                    )
                return duplicator, output_state
            except (comtypes.COMError, OSError, RuntimeError) as exc:
                hresult_u32 = self._retry_hresult_u32(
                    exc,
                    is_dxgi_backend=is_dxgi_backend,
                )
                delay_seconds = self._wait.next_delay_seconds()
                if attempt == 1 or attempt % 20 == 0:
                    if hresult_u32 is not None:
                        self._logger.info(
                            "Output recovery attempt %d failed with transient "
                            "HRESULT=0x%08X; retrying in %.3fs.",
                            attempt,
                            hresult_u32,
                            delay_seconds,
                        )
                    else:
                        self._logger.info(
                            "Output recovery attempt %d failed (%s); retrying in %.3fs.",
                            attempt,
                            exc,
                            delay_seconds,
                        )
                    self._logger.info(
                        "Recovery keeps retrying while transient display transitions "
                        "are active (exclusive/fullscreen switch, mode switch, "
                        "session disconnect/reconnect)."
                    )
                time.sleep(delay_seconds)
