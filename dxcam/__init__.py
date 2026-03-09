"""Public DXcam API.

Quick start:
    >>> import dxcam
    >>> cam = dxcam.create()
    >>> frame = cam.grab()
    >>> cam.release()

Threaded capture:
    >>> import dxcam
    >>> cam = dxcam.create(backend="dxgi")
    >>> cam.start(target_fps=60)
    >>> frame = cam.get_latest_frame()
    >>> cam.stop()
    >>> cam.release()
"""

from __future__ import annotations

import logging
import signal
import time
import weakref
from types import FrameType
from typing import Any, Callable, cast

from dxcam.core.backend import normalize_backend_name
from dxcam.dxcam import DXCamera, Output, Device
from dxcam.processor import normalize_processor_backend_name
from dxcam.types import CaptureBackend, ColorMode, ProcessorBackend, Region
from dxcam.util.io import (
    enum_dxgi_adapters,
    get_output_metadata,
)

logger = logging.getLogger(__name__)
_sigterm_handler_installed = False
_previous_sigterm_handler: int | Callable[[int, FrameType | None], Any] | None = None

__all__ = [
    "DXCamera",
    "CaptureBackend",
    "ColorMode",
    "ProcessorBackend",
    "Region",
    "create",
    "device_info",
    "output_info",
]

# Hide internal factory/signal plumbing from pdoc output.
__pdoc__: dict[str, bool] = {
    "Singleton": False,
    "DXFactory": False,
    "_configure_comtypes_logging": False,
    "_handle_sigterm": False,
    "_install_sigterm_handler": False,
    "__factory": False,
}


def _configure_comtypes_logging() -> None:
    # Suppress noisy per-frame COM Release debug logs from comtypes internals.
    logging.getLogger("comtypes").setLevel(logging.INFO)
    logging.getLogger("comtypes._post_coinit.unknwn").setLevel(logging.INFO)


class Singleton(type):
    """Metaclass that allows exactly one instance per class."""

    _instances = {}

    def __call__(cls, *args: Any, **kwargs: Any) -> Any:
        if cls not in cls._instances:
            cls._instances[cls] = super(Singleton, cls).__call__(*args, **kwargs)
        else:
            logger.warning("Only 1 instance of %s is allowed.", cls.__name__)

        return cls._instances[cls]


class DXFactory(metaclass=Singleton):
    """Factory that owns device/output discovery and camera singletons."""

    _camera_instances: weakref.WeakValueDictionary[tuple[int, int, CaptureBackend], DXCamera] = (
        weakref.WeakValueDictionary()
    )

    def __init__(self) -> None:
        p_adapters = enum_dxgi_adapters()
        self.devices: list[Device] = []
        self.outputs: list[list[Output]] = []
        for p_adapter in p_adapters:
            device = Device(p_adapter)
            p_outputs = device.enum_outputs()
            if len(p_outputs) != 0:
                self.devices.append(device)
                self.outputs.append([Output(p_output) for p_output in p_outputs])
        self.output_metadata = get_output_metadata()

    def create(
        self,
        device_idx: int = 0,
        output_idx: int | None = None,
        region: Region | None = None,
        output_color: ColorMode = "RGB",
        max_buffer_len: int = 64,
        backend: CaptureBackend = "dxgi",
        processor_backend: ProcessorBackend = "cv2",
    ) -> DXCamera:
        backend = normalize_backend_name(str(backend))
        processor_backend = normalize_processor_backend_name(str(processor_backend))
        device = self.devices[device_idx]
        if output_idx is None:
            # Select Primary Output
            primary_output_indices = [
                idx
                for idx, metadata in enumerate(
                    self.output_metadata.get(output.devicename)
                    for output in self.outputs[device_idx]
                )
                if metadata and metadata[1]
            ]
            if not primary_output_indices:
                raise RuntimeError(f"No primary output found for device index {device_idx}")
            output_idx = primary_output_indices[0]
        instance_key = (device_idx, output_idx, backend)
        existing_camera = self._camera_instances.get(instance_key)
        if existing_camera is not None and existing_camera.is_released:
            logger.info(
                "Dropping released DXCamera instance for device=%s output=%s backend=%s.",
                device_idx,
                output_idx,
                backend,
            )
            del self._camera_instances[instance_key]
            existing_camera = None
        if existing_camera is not None:
            logger.warning(
                "DXCamera instance already exists for device=%s output=%s backend=%s; "
                "returning existing instance. Delete the old object with `del obj` "
                "to recreate it with new parameters.",
                device_idx,
                output_idx,
                backend,
            )
            return existing_camera

        output = self.outputs[device_idx][output_idx]
        output.update_desc()
        camera = DXCamera(
            output=output,
            device=device,
            region=region,
            output_color=output_color,
            max_buffer_len=max_buffer_len,
            backend=backend,
            processor_backend=processor_backend,
        )
        self._camera_instances[instance_key] = camera
        time.sleep(0.1)  # Fix for https://github.com/ra1nty/DXcam/issues/31
        return camera

    def device_info(self) -> str:
        ret = ""
        for idx, device in enumerate(self.devices):
            ret += f"Device[{idx}]:{device}\n"
        return ret

    def output_info(self) -> str:
        ret = ""
        for didx, outputs in enumerate(self.outputs):
            for idx, output in enumerate(outputs):
                metadata = self.output_metadata.get(output.devicename)
                is_primary = metadata[1] if metadata else False
                ret += f"Device[{didx}] Output[{idx}]: "
                ret += f"Res:{output.resolution} Rot:{output.rotation_angle}"
                ret += f" Primary:{is_primary}\n"
        return ret

    def clean_up(self) -> None:
        for _, camera in list(self._camera_instances.items()):
            camera.release()


__factory = DXFactory()
_configure_comtypes_logging()


def _handle_sigterm(signum: int, frame: FrameType | None) -> None:
    logger.info("Received SIGTERM; releasing active DXCamera instances.")
    try:
        __factory.clean_up()
    except Exception:
        logger.exception("Failed during DXCamera SIGTERM cleanup.")

    previous = _previous_sigterm_handler
    if previous is None or previous is signal.SIG_IGN:
        return
    if previous is signal.SIG_DFL:
        raise SystemExit(128 + signum)
    if callable(previous):
        handler = cast(Callable[[int, FrameType | None], Any], previous)
        handler(signum, frame)


def _install_sigterm_handler() -> None:
    global _sigterm_handler_installed
    global _previous_sigterm_handler

    if _sigterm_handler_installed:
        return
    try:
        _previous_sigterm_handler = signal.getsignal(signal.SIGTERM)
        signal.signal(signal.SIGTERM, _handle_sigterm)
        _sigterm_handler_installed = True
    except (ValueError, AttributeError):
        # ValueError: called outside the main thread.
        # AttributeError: platform/runtime without SIGTERM.
        logger.debug("SIGTERM handler was not installed.", exc_info=True)


def create(
    device_idx: int = 0,
    output_idx: int | None = None,
    region: Region | None = None,
    output_color: ColorMode = "RGB",
    max_buffer_len: int = 64,
    backend: CaptureBackend = "dxgi",
    processor_backend: ProcessorBackend = "cv2",
) -> DXCamera:
    """Create or return a singleton camera for a device/output/backend tuple.

    Args:
        device_idx: DXGI adapter index.
        output_idx: Output index on the selected adapter. ``None`` chooses
            the primary output.
        region: Optional capture region as ``(left, top, right, bottom)``.
        output_color: Output pixel format.
        max_buffer_len: Ring-buffer size used in threaded capture mode.
        backend: Capture backend, ``"dxgi"`` or ``"winrt"``.
        processor_backend: Post-processing backend, ``"cv2"`` (default)
            or ``"numpy"``.

    Returns:
        A :class:`dxcam.dxcam.DXCamera` instance.

    Example:
        >>> import dxcam
        >>> cam = dxcam.create(
        ...     output_color="BGR",
        ...     backend="winrt",
        ...     processor_backend="cv2",
        ... )
        >>> frame = cam.grab()
        >>> cam.release()
    """
    _install_sigterm_handler()
    return __factory.create(
        device_idx=device_idx,
        output_idx=output_idx,
        region=region,
        output_color=output_color,
        max_buffer_len=max_buffer_len,
        backend=backend,
        processor_backend=processor_backend,
    )


def device_info() -> str:
    """Return a formatted list of detected DXGI adapters.

    Example:
        >>> import dxcam
        >>> print(dxcam.device_info())
    """
    return __factory.device_info()


def output_info() -> str:
    """Return a formatted list of detected outputs for each adapter.

    Example:
        >>> import dxcam
        >>> print(dxcam.output_info())
    """
    return __factory.output_info()
