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

    def create(self, device_idx=0, output_idx=None, region=None):
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
        output = self.outputs[device_idx][output_idx]
        output.update_desc()
        return DXCamera(output=output, device=device, region=region)

    def device_info(self) -> str:
        ret = "Device Info:\n"
        for idx, device in enumerate(self.devices):
            ret += f"[{idx}]:{device}"
        return ret


__factory = DXFactory()


def create(device_idx=0, output_idx=None, region=None):
    return __factory.create(device_idx, output_idx, region)


def device_info():
    return __factory.device_info()


def metadata():
    return __factory.output_metadata
