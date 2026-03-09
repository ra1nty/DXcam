from __future__ import annotations

from typing import Literal, TypeAlias

import numpy as np
from numpy.typing import NDArray

ColorMode: TypeAlias = Literal["RGB", "RGBA", "BGR", "BGRA", "GRAY"]
CaptureBackend: TypeAlias = Literal["dxgi", "winrt"]
Region: TypeAlias = tuple[int, int, int, int]
Frame: TypeAlias = NDArray[np.uint8]
