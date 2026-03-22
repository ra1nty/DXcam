from __future__ import annotations

from typing import Any, Callable, cast

from dxcam.core.device import Device
from dxcam.core.dxgi_duplicator import DXGIDuplicator
from dxcam.core.output import Output
from dxcam.types import CaptureBackend

_SUPPORTED_BACKENDS: tuple[CaptureBackend, ...] = ("dxgi", "winrt")


def _create_dxgi_duplicator(*, output: Output, device: Device) -> Any:
    return DXGIDuplicator(output=output, device=device)


def _create_winrt_duplicator(
    *, output: Output, device: Device, target_hwnd: int | None = None
) -> Any:
    from dxcam.core.winrt_duplicator import WinRTDuplicator

    return WinRTDuplicator(output=output, device=device, target_hwnd=target_hwnd)


_BACKEND_CREATORS: dict[
    CaptureBackend, Callable[..., Any]
] = {
    "dxgi": _create_dxgi_duplicator,
    "winrt": _create_winrt_duplicator,
}


def normalize_backend_name(backend: str) -> CaptureBackend:
    """Normalize and validate a capture backend name.

    Args:
        backend: Backend name provided by user input.

    Returns:
        Lower-cased validated backend literal (``"dxgi"`` or ``"winrt"``).

    Raises:
        ValueError: If ``backend`` is not a supported capture backend.
    """
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
    target_hwnd: int | None = None,
) -> Any:
    """Create a backend-specific duplicator instance.

    Args:
        backend: Selected capture backend.
        output: Output descriptor to capture from.
        device: Device descriptor associated with ``output``.
        target_hwnd: Optional window handle for WinRT window capture. Ignored for DXGI.

    Returns:
        A duplicator instance that implements the capture backend contract.

    Raises:
        ValueError: If ``backend`` has no registered factory.
    """
    creator = _BACKEND_CREATORS.get(backend)
    if creator is None:
        # Defensive fallback in case literals are expanded without wiring.
        raise ValueError(f"Unsupported backend '{backend}'.")
    if backend == "winrt" and target_hwnd is not None:
        return creator(output=output, device=device, target_hwnd=target_hwnd)
    return creator(output=output, device=device)
