from __future__ import annotations

from typing import Any, Callable, cast

from dxcam.core.device import Device
from dxcam.core.dxgi_duplicator import DXGIDuplicator
from dxcam.core.output import Output
from dxcam.types import CaptureBackend

_SUPPORTED_BACKENDS: tuple[CaptureBackend, ...] = ("dxgi", "winrt")


def _create_dxgi_duplicator(*, output: Output, device: Device) -> Any:
    return DXGIDuplicator(output=output, device=device)


def _create_winrt_duplicator(*, output: Output, device: Device) -> Any:
    from dxcam.core.winrt_duplicator import WinRTDuplicator

    return WinRTDuplicator(output=output, device=device)


_BACKEND_CREATORS: dict[
    CaptureBackend, Callable[..., Any]
] = {
    "dxgi": _create_dxgi_duplicator,
    "winrt": _create_winrt_duplicator,
}


def normalize_backend_name(backend: str) -> CaptureBackend:
    normalized = backend.lower()
    if normalized not in _SUPPORTED_BACKENDS:
        supported = ", ".join(_SUPPORTED_BACKENDS)
        raise ValueError(f"Unsupported backend '{backend}'. Supported: {supported}.")
    return cast(CaptureBackend, normalized)


def create_backend_duplicator(
    backend: CaptureBackend,
    *,
    output: Output,
    device: Device,
) -> Any:
    creator = _BACKEND_CREATORS.get(backend)
    if creator is None:
        # Defensive fallback in case literals are expanded without wiring.
        raise ValueError(f"Unsupported backend '{backend}'.")
    return creator(output=output, device=device)
