"""Public type aliases used by DXcam APIs.

Example:
    >>> from dxcam.types import CaptureBackend, Region
    >>> backend: CaptureBackend = "dxgi"
    >>> region: Region = (0, 0, 1920, 1080)
"""

from __future__ import annotations

from typing import Literal, TypeAlias

import numpy as np
from numpy.typing import NDArray

#: Output pixel format accepted by :func:`dxcam.create`.
#:
#: Example:
#:     >>> mode: ColorMode = "BGRA"
ColorMode: TypeAlias = Literal["RGB", "RGBA", "BGR", "BGRA", "GRAY"]

#: Capture backend accepted by :func:`dxcam.create`.
#:
#: Example:
#:     >>> backend: CaptureBackend = "dxgi"
CaptureBackend: TypeAlias = Literal["dxgi", "winrt"]

#: Rectangle tuple ``(left, top, right, bottom)`` in output coordinates.
#:
#: Example:
#:     >>> region: Region = (0, 0, 1920, 1080)
Region: TypeAlias = tuple[int, int, int, int]

#: Captured frame as ``numpy.ndarray`` with ``dtype=uint8``.
#:
#: Example:
#:     >>> import numpy as np
#:     >>> frame: Frame = np.zeros((1080, 1920, 3), dtype=np.uint8)
Frame: TypeAlias = NDArray[np.uint8]

__all__ = ["ColorMode", "CaptureBackend", "Region", "Frame"]
