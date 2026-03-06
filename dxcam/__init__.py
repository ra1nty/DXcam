from __future__ import annotations

import logging
import weakref
import time
from typing import Any

from dxcam.dxcam import DXCamera, Output, Device
from dxcam.types import ColorMode, Region
from dxcam.util.io import (
    enum_dxgi_adapters,
    get_output_metadata,
)

logger = logging.getLogger(__name__)


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

    _camera_instances: weakref.WeakValueDictionary[tuple[int, int], DXCamera] = (
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
    ) -> DXCamera:
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
        instance_key = (device_idx, output_idx)
        if instance_key in self._camera_instances:
            logger.warning(
                "DXCamera instance already exists for device=%s output=%s; "
                "returning existing instance. Delete the old object with `del obj` "
                "to recreate it with new parameters.",
                device_idx,
                output_idx,
            )
            return self._camera_instances[instance_key]

        output = self.outputs[device_idx][output_idx]
        output.update_desc()
        camera = DXCamera(
            output=output,
            device=device,
            region=region,
            output_color=output_color,
            max_buffer_len=max_buffer_len,
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
        for _, camera in self._camera_instances.items():
            camera.release()


__factory = DXFactory()


def create(
    device_idx: int = 0,
    output_idx: int | None = None,
    region: Region | None = None,
    output_color: ColorMode = "RGB",
    max_buffer_len: int = 64,
) -> DXCamera:
    return __factory.create(
        device_idx=device_idx,
        output_idx=output_idx,
        region=region,
        output_color=output_color,
        max_buffer_len=max_buffer_len,
    )


def device_info() -> str:
    return __factory.device_info()


def output_info() -> str:
    return __factory.output_info()
