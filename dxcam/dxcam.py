import time
import ctypes
from dxcam._libs.dxgi import IDXGISurface, DXGI_MAPPED_RECT
from dxcam.core import Device, Output, StageSurface, Duplicator
from dxcam.processor import Processor


class DxOutputDuplicator:

    _device: Device
    _output: Output
    _stagesuf: StageSurface
    _duplicator: Duplicator
    _processor: Processor
    region: tuple[int, int, int, int]
    width: int
    height: int
    rotation_angle: int = 0

    def __init__(
        self,
        output: Output,
        device: Device,
        region: tuple[int, int, int, int],
    ) -> None:
        self.region = region

        self._output = output
        self._device = device

        self.width, self.height = self._output.resolution
        self.rotation_angle = self._output.rotation_angle
        if self.region is None:
            self.region = (0, 0, self.width, self.height)

        self._stagesuf = StageSurface(output=self._output, device=self._device)
        self._duplicator = Duplicator(output=self._output, device=self._device)
        self._processor = Processor()

    def capture(self):
        if self._duplicator.update_frame():
            if not self._duplicator.updated:
                return None
            self._device.im_context.CopyResource(
                self._stagesuf.texture, self._duplicator.texture
            )
            self._duplicator.release_frame()
            surf = self._stagesuf.texture.QueryInterface(IDXGISurface)
            rect = DXGI_MAPPED_RECT()
            surf.Map(ctypes.byref(rect), 1)

            frame = self._processor.process(
                rect, self.width, self.height, self.region, self.rotation_angle
            )
            surf.Unmap()
            return frame
        else:
            time.sleep(0.5)
            self._duplicator.release()
            self._stagesuf.release()
            self._output.update_desc()
            self.width, self.height = self._output.resolution
            self.region = (0, 0, self.width, self.height)
            self.rotation_angle = self._output.rotation_angle
            self._stagesuf.rebuild(output=self._output, device=self._device)
            self._duplicator = Duplicator(output=self._output, device=self._device)
            return None

    def release(self):
        self._duplicator.release()
        self._stagesuf.release()

    def __repr__(self) -> str:
        ret = f"DxOutputDuplicator:\n"
        ret += f"\tDevice:\t {self._device}"
        ret += f"\tOutput:\t {self._output}"
        ret += f"\tProcessor:\t {self._processor}"
        return ret
