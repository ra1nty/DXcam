"""Backward-compatible import shim for DXGI duplicator.

Prefer importing DXGIDuplicator from ``dxcam.core.dxgi_duplicator``.
"""

from __future__ import annotations

import warnings

from dxcam.core.dxgi_duplicator import DXGIDuplicator

warnings.warn(
    "dxcam.core.duplicator is deprecated and will be removed in a future release. "
    "Import DXGIDuplicator from dxcam.core.dxgi_duplicator instead.",
    FutureWarning,
    stacklevel=2,
)

# Backward compatibility alias.
Duplicator = DXGIDuplicator

__all__ = ["DXGIDuplicator", "Duplicator"]
