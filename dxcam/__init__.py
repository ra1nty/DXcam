import signal
import weakref
from collections import defaultdict
from dxcam.dxcam import DXCamera, Output, Device
from dxcam.util.io import (
    enum_dxgi_adapters,
    get_output_metadata,
    get_monitor_name_by_handle,
)


class Singleton(type):
    _instances = {}

    def __call__(cls, *args, **kwargs):
        if cls not in cls._instances:
            cls._instances[cls] = super(Singleton, cls).__call__(*args, **kwargs)
        else:
            print(f"Only 1 instance of {cls.__name__} is allowed.")

        return cls._instances[cls]


class DXFactory(metaclass=Singleton):

    _camera_instances = weakref.WeakValueDictionary()

    def __init__(self) -> None:
        p_adapters = enum_dxgi_adapters()
        self.devices, self.outputs = [], []
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
        output_idx: int = None,
        region: tuple = None,
        output_color: str = "RGB",
        max_buffer_len: int = 64,
    ):
        device = self.devices[device_idx]
        if output_idx is None:
            # Select Primary Output
            output_idx = [
                idx
                for idx, metadata in enumerate(
                    self.output_metadata.get(output.devicename)
                    for output in self.outputs[device_idx]
                )
                if metadata[1]
            ][0]
        instance_key = (device_idx, output_idx)
        if instance_key in self._camera_instances:
            print(
                "".join(
                    (
                        f"You already created a DXCamera Instance for Device {device_idx}--Output {output_idx}!\n",
                        "Returning the existed instance...\n",
                        "To change capture parameters you can manually delete the old object using `del obj`.",
                    )
                )
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
        return camera

    def device_info(self) -> str:
        ret = "Device Info:\n"
        for idx, device in enumerate(self.devices):
            ret += f"[{idx}]:{device}"
        return ret

    def clean_up(self):
        for _, camera in self._camera_instances.items():
            camera.release()


__factory = DXFactory()


def create(
    device_idx: int = 0,
    output_idx: int = None,
    region: tuple = None,
    output_color: str = "RGB",
    max_buffer_len: int = 64,
):
    return __factory.create(
        device_idx=device_idx,
        output_idx=output_idx,
        region=region,
        output_color=output_color,
        max_buffer_len=max_buffer_len,
    )


def device_info():
    return __factory.device_info()


def metadata():
    return __factory.output_metadata


def signal_handler(sig, frame):
    __factory.clean_up()

signal.signal(signal.SIGINT, signal_handler)
