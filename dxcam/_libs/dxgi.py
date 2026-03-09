import ctypes
import ctypes.wintypes as wintypes
import comtypes
from .d3d11 import ID3D11Device


DXGI_ERROR_ACCESS_LOST = 0x887A0026
DXGI_ERROR_NOT_FOUND = 0x887A0002
DXGI_ERROR_SESSION_DISCONNECTED = 0x887A0028
DXGI_ERROR_WAIT_TIMEOUT = 0x887A0027

DXGI_OUTDUPL_FLAG_NONE = 0x0
DXGI_OUTDUPL_FLAG_COMPOSITED_UI_CAPTURE_ONLY = 0x1


class LUID(ctypes.Structure):
    _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]


class DXGI_ADAPTER_DESC1(ctypes.Structure):
    _fields_ = [
        ("Description", wintypes.WCHAR * 128),
        ("VendorId", wintypes.UINT),
        ("DeviceId", wintypes.UINT),
        ("SubSysId", wintypes.UINT),
        ("Revision", wintypes.UINT),
        ("DedicatedVideoMemory", wintypes.ULARGE_INTEGER),
        ("DedicatedSystemMemory", wintypes.ULARGE_INTEGER),
        ("SharedSystemMemory", wintypes.ULARGE_INTEGER),
        ("AdapterLuid", LUID),
        ("Flags", wintypes.UINT),
    ]


class DXGI_OUTPUT_DESC(ctypes.Structure):
    _fields_ = [
        ("DeviceName", wintypes.WCHAR * 32),
        ("DesktopCoordinates", wintypes.RECT),
        ("AttachedToDesktop", wintypes.BOOL),
        ("Rotation", wintypes.UINT),
        ("Monitor", wintypes.HMONITOR),
    ]


class DXGI_OUTDUPL_POINTER_POSITION(ctypes.Structure):
    _fields_ = [("Position", wintypes.POINT), ("Visible", wintypes.BOOL)]


class DXGI_OUTDUPL_FRAME_INFO(ctypes.Structure):
    _fields_ = [
        ("LastPresentTime", wintypes.LARGE_INTEGER),
        ("LastMouseUpdateTime", wintypes.LARGE_INTEGER),
        ("AccumulatedFrames", wintypes.UINT),
        ("RectsCoalesced", wintypes.BOOL),
        ("ProtectedContentMaskedOut", wintypes.BOOL),
        ("PointerPosition", DXGI_OUTDUPL_POINTER_POSITION),
        ("TotalMetadataBufferSize", wintypes.UINT),
        ("PointerShapeBufferSize", wintypes.UINT),
    ]


class DXGI_MAPPED_RECT(ctypes.Structure):
    _fields_ = [("Pitch", wintypes.INT), ("pBits", ctypes.POINTER(ctypes.c_ubyte))]


class IDXGIObject(comtypes.IUnknown):
    _iid_ = comtypes.GUID("{aec22fb8-76f3-4639-9be0-28eb43a67a2e}")
    _methods_ = [
        comtypes.STDMETHOD(comtypes.HRESULT, "SetPrivateData"),
        comtypes.STDMETHOD(comtypes.HRESULT, "SetPrivateDataInterface"),
        comtypes.STDMETHOD(comtypes.HRESULT, "GetPrivateData"),
        comtypes.STDMETHOD(comtypes.HRESULT, "GetParent"),
    ]


class IDXGIDeviceSubObject(IDXGIObject):
    _iid_ = comtypes.GUID("{3d3e0379-f9de-4d58-bb6c-18d62992f1a6}")
    _methods_ = [
        comtypes.STDMETHOD(comtypes.HRESULT, "GetDevice"),
    ]


class IDXGIResource(IDXGIDeviceSubObject):
    _iid_ = comtypes.GUID("{035f3ab4-482e-4e50-b41f-8a7f8bd8960b}")
    _methods_ = [
        comtypes.STDMETHOD(comtypes.HRESULT, "GetSharedHandle"),
        comtypes.STDMETHOD(comtypes.HRESULT, "GetUsage"),
        comtypes.STDMETHOD(comtypes.HRESULT, "SetEvictionPriority"),
        comtypes.STDMETHOD(comtypes.HRESULT, "GetEvictionPriority"),
    ]


class IDXGISurface(IDXGIDeviceSubObject):
    _iid_ = comtypes.GUID("{cafcb56c-6ac3-4889-bf47-9e23bbd260ec}")
    _methods_ = [
        comtypes.STDMETHOD(comtypes.HRESULT, "GetDesc"),
        comtypes.STDMETHOD(
            comtypes.HRESULT, "Map", [ctypes.POINTER(DXGI_MAPPED_RECT), wintypes.UINT]
        ),
        comtypes.STDMETHOD(comtypes.HRESULT, "Unmap"),
    ]


class IDXGIDevice(IDXGIObject):
    _iid_ = comtypes.GUID("{54ec77fa-1377-44e6-8c32-88fd5f44c84c}")
    _methods_ = [
        comtypes.STDMETHOD(comtypes.HRESULT, "GetAdapter"),
        comtypes.STDMETHOD(comtypes.HRESULT, "CreateSurface"),
        comtypes.STDMETHOD(comtypes.HRESULT, "QueryResourceResidency"),
        comtypes.STDMETHOD(comtypes.HRESULT, "SetGPUThreadPriority"),
        comtypes.STDMETHOD(comtypes.HRESULT, "GetGPUThreadPriority"),
    ]


class IDXGIOutputDuplication(IDXGIObject):
    _iid_ = comtypes.GUID("{191cfac3-a341-470d-b26e-a864f428319c}")
    _methods_ = [
        comtypes.STDMETHOD(None, "GetDesc"),
        comtypes.STDMETHOD(
            comtypes.HRESULT,
            "AcquireNextFrame",
            [
                wintypes.UINT,
                ctypes.POINTER(DXGI_OUTDUPL_FRAME_INFO),
                ctypes.POINTER(ctypes.POINTER(IDXGIResource)),
            ],
        ),
        comtypes.STDMETHOD(comtypes.HRESULT, "GetFrameDirtyRects"),
        comtypes.STDMETHOD(comtypes.HRESULT, "GetFrameMoveRects"),
        comtypes.STDMETHOD(comtypes.HRESULT, "GetFramePointerShape"),
        comtypes.STDMETHOD(comtypes.HRESULT, "MapDesktopSurface"),
        comtypes.STDMETHOD(comtypes.HRESULT, "UnMapDesktopSurface"),
        comtypes.STDMETHOD(comtypes.HRESULT, "ReleaseFrame"),
    ]


class IDXGIOutput(IDXGIObject):
    _iid_ = comtypes.GUID("{ae02eedb-c735-4690-8d52-5a8dc20213aa}")
    _methods_ = [
        comtypes.STDMETHOD(
            comtypes.HRESULT, "GetDesc", [ctypes.POINTER(DXGI_OUTPUT_DESC)]
        ),
        comtypes.STDMETHOD(comtypes.HRESULT, "GetDisplayModeList"),
        comtypes.STDMETHOD(comtypes.HRESULT, "FindClosestMatchingMode"),
        comtypes.STDMETHOD(comtypes.HRESULT, "WaitForVBlank"),
        comtypes.STDMETHOD(comtypes.HRESULT, "TakeOwnership"),
        comtypes.STDMETHOD(None, "ReleaseOwnership"),
        comtypes.STDMETHOD(comtypes.HRESULT, "GetGammaControlCapabilities"),
        comtypes.STDMETHOD(comtypes.HRESULT, "SetGammaControl"),
        comtypes.STDMETHOD(comtypes.HRESULT, "GetGammaControl"),
        comtypes.STDMETHOD(comtypes.HRESULT, "SetDisplaySurface"),
        comtypes.STDMETHOD(comtypes.HRESULT, "GetDisplaySurfaceData"),
        comtypes.STDMETHOD(comtypes.HRESULT, "GetFrameStatistics"),
    ]


class IDXGIOutput1(IDXGIOutput):
    _iid_ = comtypes.GUID("{00cddea8-939b-4b83-a340-a685226666cc}")
    _methods_ = [
        comtypes.STDMETHOD(comtypes.HRESULT, "GetDisplayModeList1"),
        comtypes.STDMETHOD(comtypes.HRESULT, "FindClosestMatchingMode1"),
        comtypes.STDMETHOD(comtypes.HRESULT, "GetDisplaySurfaceData1"),
        comtypes.STDMETHOD(
            comtypes.HRESULT,
            "DuplicateOutput",
            [
                ctypes.POINTER(ID3D11Device),
                ctypes.POINTER(ctypes.POINTER(IDXGIOutputDuplication)),
            ],
        ),
    ]


class IDXGIOutput2(IDXGIOutput1):
    _iid_ = comtypes.GUID("{595e39d1-2724-4663-99b1-da969de28364}")
    _methods_ = [
        comtypes.STDMETHOD(wintypes.BOOL, "SupportsOverlays"),
    ]


class IDXGIOutput3(IDXGIOutput2):
    _iid_ = comtypes.GUID("{8a6bb301-7e7e-41f4-a8e0-5b32f7f99b18}")
    _methods_ = [
        comtypes.STDMETHOD(
            comtypes.HRESULT,
            "CheckOverlaySupport",
            [wintypes.UINT, ctypes.c_void_p, ctypes.POINTER(wintypes.UINT)],
        ),
    ]


class IDXGIOutput4(IDXGIOutput3):
    _iid_ = comtypes.GUID("{dc7dca35-2196-414d-9f53-617884032a60}")
    _methods_ = [
        comtypes.STDMETHOD(
            comtypes.HRESULT,
            "CheckOverlayColorSpaceSupport",
            [
                wintypes.UINT,
                wintypes.UINT,
                ctypes.c_void_p,
                ctypes.POINTER(wintypes.UINT),
            ],
        ),
    ]


class IDXGIOutput5(IDXGIOutput4):
    _iid_ = comtypes.GUID("{80a07424-ab52-42eb-833c-0c42fd282d98}")
    _methods_ = [
        comtypes.STDMETHOD(
            comtypes.HRESULT,
            "DuplicateOutput1",
            [
                ctypes.c_void_p,
                wintypes.UINT,
                wintypes.UINT,
                ctypes.POINTER(wintypes.UINT),
                ctypes.POINTER(ctypes.POINTER(IDXGIOutputDuplication)),
            ],
        ),
    ]


class IDXGIAdapter(IDXGIObject):
    _iid_ = comtypes.GUID("{2411e7e1-12ac-4ccf-bd14-9798e8534dc0}")
    _methods_ = [
        comtypes.STDMETHOD(
            comtypes.HRESULT,
            "EnumOutputs",
            [wintypes.UINT, ctypes.POINTER(ctypes.POINTER(IDXGIOutput))],
        ),
        comtypes.STDMETHOD(comtypes.HRESULT, "GetDesc"),
        comtypes.STDMETHOD(comtypes.HRESULT, "CheckInterfaceSupport"),
    ]


class IDXGIAdapter1(IDXGIAdapter):
    _iid_ = comtypes.GUID("{29038f61-3839-4626-91fd-086879011a05}")
    _methods_ = [
        comtypes.STDMETHOD(
            comtypes.HRESULT, "GetDesc1", [ctypes.POINTER(DXGI_ADAPTER_DESC1)]
        ),
    ]


class IDXGIFactory(IDXGIObject):
    _iid_ = comtypes.GUID("{7b7166ec-21c7-44ae-b21a-c9ae321ae369}")
    _methods_ = [
        comtypes.STDMETHOD(comtypes.HRESULT, "EnumAdapters"),
        comtypes.STDMETHOD(comtypes.HRESULT, "MakeWindowAssociation"),
        comtypes.STDMETHOD(comtypes.HRESULT, "GetWindowAssociation"),
        comtypes.STDMETHOD(comtypes.HRESULT, "CreateSwapChain"),
        comtypes.STDMETHOD(comtypes.HRESULT, "CreateSoftwareAdapter"),
    ]


class IDXGIFactory1(IDXGIFactory):
    _iid_ = comtypes.GUID("{770aae78-f26f-4dba-a829-253c83d1b387}")
    _methods_ = [
        comtypes.STDMETHOD(
            comtypes.HRESULT,
            "EnumAdapters1",
            [ctypes.c_uint, ctypes.POINTER(ctypes.POINTER(IDXGIAdapter1))],
        ),
        comtypes.STDMETHOD(wintypes.BOOL, "IsCurrent"),
    ]
