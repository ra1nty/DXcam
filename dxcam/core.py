import time
import ctypes
from dataclasses import InitVar, dataclass
import comtypes
from dxcam.libs.d3d11 import *
from dxcam.libs.dxgi import *
from dxcam.processor import Processor


@dataclass
class Device:
    adapter: ctypes.POINTER(IDXGIAdapter1)
    device: ctypes.POINTER(ID3D11Device) = None
    context: ctypes.POINTER(ID3D11DeviceContext) = None
    im_context: ctypes.POINTER(ID3D11DeviceContext) = None
    desc: DXGI_ADAPTER_DESC1 = None

    def __post_init__(self) -> None:
        self.desc = DXGI_ADAPTER_DESC1()
        self.adapter.GetDesc1(ctypes.byref(self.desc))

        D3D11CreateDevice = ctypes.windll.d3d11.D3D11CreateDevice

        feature_levels = [
            D3D_FEATURE_LEVEL_11_0,
            D3D_FEATURE_LEVEL_10_1,
            D3D_FEATURE_LEVEL_10_0,
        ]

        self.device = ctypes.POINTER(ID3D11Device)()
        self.context = ctypes.POINTER(ID3D11DeviceContext)()
        self.im_context = ctypes.POINTER(ID3D11DeviceContext)()

        D3D11CreateDevice(
            self.adapter,
            0,
            None,
            0,
            ctypes.byref((ctypes.c_uint * len(feature_levels))(*feature_levels)),
            len(feature_levels),
            7,
            ctypes.byref(self.device),
            None,
            ctypes.byref(self.context),
        )
        self.device.GetImmediateContext(ctypes.byref(self.im_context))

    def enum_outputs(self) -> list[ctypes.POINTER(IDXGIOutput1)]:
        i = 0
        p_outputs = list()
        while True:
            try:
                p_output = ctypes.POINTER(IDXGIOutput1)()
                self.adapter.EnumOutputs(i, ctypes.byref(p_output))
                p_outputs.append(p_output)
                i += 1
            except comtypes.COMError as ce:
                if ctypes.c_int32(DXGI_ERROR_NOT_FOUND).value == ce.args[0]:
                    break
                else:
                    raise ce
        return p_outputs

    @property
    def description(self) -> str:
        return self.desc.Description


class Output:
    def __init__(self, output: ctypes.POINTER(IDXGIOutput1)):
        self.output = output
        self.desc: DXGI_OUTPUT_DESC = DXGI_OUTPUT_DESC()
        self.output.GetDesc(ctypes.byref(self.desc))
        self.rotation_mapping: dict = {0: 0, 1: 0, 2: 90, 3: 180, 4: 270}

    @property
    def hmonitor(self) -> wintypes.HMONITOR:
        return self.desc.Monitor

    @property
    def devicename(self) -> str:
        return self.desc.DeviceName

    @property
    def resolution(self) -> tuple[int, int]:
        return (
            (self.desc.DesktopCoordinates.right - self.desc.DesktopCoordinates.left),
            (self.desc.DesktopCoordinates.bottom - self.desc.DesktopCoordinates.top),
        )

    @property
    def attached_to_desktop(self) -> bool:
        return bool(self.desc.AttachedToDesktop)

    @property
    def rotation_angle(self) -> int:
        return self.rotation_mapping.get(self.desc.Rotation, 0)

    def __repr__(self) -> str:
        repr = f"{self.devicename}, {self.resolution}, {self.attached_to_desktop}, {self.rotation_angle}"
        return repr


@dataclass
class StageSurface:
    width: ctypes.c_uint32
    height: ctypes.c_uint32
    dxgi_format: ctypes.c_uint32 = DXGI_FORMAT_B8G8R8A8_UNORM
    desc: D3D11_TEXTURE2D_DESC = D3D11_TEXTURE2D_DESC()
    texture: ctypes.POINTER(ID3D11Texture2D) = None
    device: InitVar[ctypes.POINTER(ID3D11Device)] = None

    def __post_init__(self, device) -> None:
        self.rebuild(device)

    def release(self):
        self.texture.Release()
        self.texture = None

    def rebuild(self, device: ctypes.POINTER(ID3D11Device)):
        if self.texture is None:
            self.desc.Width = self.width
            self.desc.Height = self.height
            self.desc.Format = self.dxgi_format
            self.desc.MipLevels = 1
            self.desc.ArraySize = 1
            self.desc.SampleDesc.Count = 1
            self.desc.SampleDesc.Quality = 0
            self.desc.Usage = D3D11_USAGE_STAGING
            self.desc.CPUAccessFlags = D3D11_CPU_ACCESS_READ
            self.desc.MiscFlags = 0
            self.desc.BindFlags = 0
            self.texture = ctypes.POINTER(ID3D11Texture2D)()
            device.CreateTexture2D(
                ctypes.byref(self.desc),
                None,
                ctypes.byref(self.texture),
            )

    def __repr__(self) -> str:
        repr = f"{self.width}, {self.height}, {self.dxgi_format}"
        return repr


@dataclass
class Duplicator:
    texture: ctypes.POINTER(ID3D11Texture2D) = ctypes.POINTER(ID3D11Texture2D)()
    duplicator: ctypes.POINTER(IDXGIOutputDuplication) = None
    updated: bool = False
    output: InitVar[ctypes.POINTER(IDXGIOutput1)] = None
    device: InitVar[ctypes.POINTER(ID3D11Device)] = None

    def __post_init__(self, output, device) -> None:
        self.duplicator = ctypes.POINTER(IDXGIOutputDuplication)()
        output.DuplicateOutput(device, ctypes.byref(self.duplicator))

    def update_frame(self):
        info = DXGI_OUTDUPL_FRAME_INFO()
        res = ctypes.POINTER(IDXGIResource)()
        try:
            self.duplicator.AcquireNextFrame(
                0,
                ctypes.byref(info),
                ctypes.byref(res),
            )
        except comtypes.COMError as ce:
            if ctypes.c_int32(DXGI_ERROR_ACCESS_LOST).value == ce.args[0]:
                return False
            if ctypes.c_int32(DXGI_ERROR_WAIT_TIMEOUT).value == ce.args[0]:
                self.updated = False
                return True
            else:
                raise ce
        try:
            self.texture = res.QueryInterface(ID3D11Texture2D)
        except comtypes.COMError as ce:
            self.duplicator.ReleaseFrame()
        self.updated = True
        return True

    def release_frame(self):
        self.duplicator.ReleaseFrame()

    def release(self):
        self.duplicator.Release()
        self.duplicator = None


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
        device: Device,
        output: Output,
        region: tuple[int, int, int, int],
    ) -> None:
        self.region = region
        self._device = device
        self._output = output

        self.width, self.height = self._output.resolution
        self.rotation_angle = self._output.rotation_angle

        if self.region is None:
            self.region = (0, 0, self.width, self.height)

        self._stagesuf = StageSurface(
            width=self.width, height=self.height, device=self._device.device
        )
        self._duplicator = Duplicator(
            output=self._output.output, device=self._device.device
        )
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
            self._duplicator = Duplicator(
                output=self._output.output, device=self._device.device
            )
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
