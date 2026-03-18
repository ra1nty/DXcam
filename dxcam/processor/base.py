from __future__ import annotations

from typing import Any, cast

import numpy as np
from numpy.typing import NDArray
from dxcam.types import ColorMode, ProcessorBackend, Region

_SUPPORTED_PROCESSOR_BACKENDS: tuple[ProcessorBackend, ...] = ("cv2", "numpy", "cython")


def normalize_processor_backend_name(backend: str) -> ProcessorBackend:
    """Normalize and validate a processor backend name.

    Args:
        backend: Backend name provided by user input.

    Returns:
        Lower-cased validated backend literal.

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
    - ``cython``: Direct Cython rotate/crop/convert path without cv2.
    """

    def __init__(
        self,
        backend: ProcessorBackend = "cv2",
        output_color: ColorMode = "RGB",
    ) -> None:
        self._impl = self._create_impl(
            normalize_processor_backend_name(backend),
            output_color,
        )

    def process(
        self,
        rect: Any,
        width: int,
        height: int,
        region: Region,
        rotation_angle: int,
    ) -> NDArray[np.uint8]:
        return self._impl.process(rect, width, height, region, rotation_angle)

    def process_into(
        self,
        rect: Any,
        width: int,
        height: int,
        region: Region,
        rotation_angle: int,
        dst: NDArray[np.uint8],
    ) -> None:
        self._impl.process_into(rect, width, height, region, rotation_angle, dst)

    @staticmethod
    def _create_impl(
        backend: ProcessorBackend,
        output_color: ColorMode,
    ) -> Any:
        if backend == "cython":
            from dxcam.processor.cython_processor import CythonProcessor

            return CythonProcessor(output_color)
        if backend == "cv2":
            from dxcam.processor.cv2_processor import Cv2Processor

            return Cv2Processor(output_color)
        if backend == "numpy":
            from dxcam.processor.numpy_processor import NumpyProcessor

            return NumpyProcessor(output_color)
        raise ValueError(f"Unsupported processor backend: {backend}")
