from .dxcam import DXCam

factory = DXCam()


def create(device_idx=0, output_idx=None, region=None):
    return factory.create(device_idx, output_idx, region)
