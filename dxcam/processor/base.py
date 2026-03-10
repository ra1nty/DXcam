from __future__ import annotations

import enum
from typing import Any, cast

import numpy as np
from numpy.typing import NDArray
from dxcam.types import ColorMode, ProcessorBackend, Region


class ProcessorBackends(enum.Enum):
    """Concrete processor backend implementations used by :class:`Processor`."""

    PIL = 0
    CV2 = 1
    NUMPY = 2


_SUPPORTED_PROCESSOR_BACKENDS: tuple[ProcessorBackend, ...] = ("cv2", "numpy")


def normalize_processor_backend_name(backend: str) -> ProcessorBackend:
    """Normalize and validate a processor backend name.

    Args:
        backend: Backend name provided by user input.

    Returns:
        Lower-cased validated backend literal (``"cv2"`` or ``"numpy"``).

    Raises:
        ValueError: If ``backend`` is not a supported processor backend.
    """
    normalized = backend.lower()
    if normalized not in _SUPPORTED_PROCESSOR_BACKENDS:
        supported = ", ".join(_SUPPORTED_PROCESSOR_BACKENDS)
        raise ValueError(
            f"Unsupported processor backend '{backend}'. Supported: {supported}."
        )
    return cast(ProcessorBackend, normalized)


class Processor:
    """Color conversion and post-processing backend selector.

    This wrapper dispatches to one concrete backend:
    - ``cv2``: OpenCV-based conversion path (default).
    - ``numpy``: Cython-accelerated conversion path with cv2 fallback.
    """

    def __init__(
        self,
        backend: ProcessorBackends | ProcessorBackend = ProcessorBackends.CV2,
        output_color: ColorMode = "RGB",
    ) -> None:
        """Create a processor dispatcher.

        Args:
            backend: Processor backend enum or backend name.
            output_color: Desired output color mode.
        """
        if isinstance(backend, str):
            backend_name = normalize_processor_backend_name(backend)
            if backend_name == "cv2":
                backend = ProcessorBackends.CV2
            elif backend_name == "numpy":
                backend = ProcessorBackends.NUMPY
            else:
                raise ValueError(f"Unsupported processor backend: {backend_name}")
        self.color_mode = output_color
        self.backend = self._initialize_backend(backend)

    def process(
        self,
        rect: Any,
        width: int,
        height: int,
        region: Region,
        rotation_angle: int,
    ) -> NDArray[np.uint8]:
        """Return a processed frame.

        Args:
            rect: Backend-specific mapped frame object.
            width: Active frame width in pixels.
            height: Active frame height in pixels.
            region: Capture region as ``(left, top, right, bottom)``.
            rotation_angle: Output rotation in degrees.

        The returned array may be backed by an internal reusable buffer.
        Use :meth:`process_into` when the caller must own destination memory.
        """
        return self.backend.process(rect, width, height, region, rotation_angle)

    def process_into(
        self,
        rect: Any,
        width: int,
        height: int,
        region: Region,
        rotation_angle: int,
        dst: NDArray[np.uint8],
    ) -> None:
        """Process into caller-provided destination array ``dst``.

        Args:
            rect: Backend-specific mapped frame object.
            width: Active frame width in pixels.
            height: Active frame height in pixels.
            region: Capture region as ``(left, top, right, bottom)``.
            rotation_angle: Output rotation in degrees.
            dst: Destination array to write into.
        """
        self.backend.process_into(rect, width, height, region, rotation_angle, dst)

    def _initialize_backend(self, backend: ProcessorBackends) -> Any:
        if backend == ProcessorBackends.CV2:
            from dxcam.processor.cv2_processor import Cv2Processor

            return Cv2Processor(self.color_mode)
        if backend == ProcessorBackends.NUMPY:
            from dxcam.processor.numpy_processor import NumpyProcessor

            return NumpyProcessor(self.color_mode)
        raise ValueError(f"Unsupported processor backend: {backend}")
