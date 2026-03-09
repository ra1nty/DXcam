__all__ = ["Device", "Output", "StageSurface", "DXGIDuplicator", "Duplicator"]


from dxcam.core.device import Device
from dxcam.core.output import Output
from dxcam.core.stagesurf import StageSurface
from dxcam.core.dxgi_duplicator import DXGIDuplicator

# Backward compatibility alias for older imports.
Duplicator = DXGIDuplicator
