from __future__ import annotations

import enum
from typing import Any

import numpy as np
from numpy.typing import NDArray
from dxcam.types import ColorMode, Region


class ProcessorBackends(enum.Enum):
    PIL = 0
    NUMPY = 1


class Processor:
    """Color conversion and post-processing backend selector."""

    def __init__(
        self,
        backend: ProcessorBackends = ProcessorBackends.NUMPY,
        output_color: ColorMode = "RGB",
    ) -> None:
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
        self.backend.process_into(rect, width, height, region, rotation_angle, dst)

    def _initialize_backend(self, backend: ProcessorBackends) -> Any:
        if backend == ProcessorBackends.NUMPY:
            from dxcam.processor.numpy_processor import NumpyProcessor

            return NumpyProcessor(self.color_mode)
        raise ValueError(f"Unsupported processor backend: {backend}")
