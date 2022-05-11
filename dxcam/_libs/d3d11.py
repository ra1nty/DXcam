import ctypes
import ctypes.wintypes as wintypes
import comtypes


D3D11_CPU_ACCESS_WRITE = 0x10000
D3D11_CPU_ACCESS_READ = 0x20000

D3D_FEATURE_LEVEL_9_1 = 0x9100
D3D_FEATURE_LEVEL_9_2 = 0x9200
D3D_FEATURE_LEVEL_9_3 = 0x9300
D3D_FEATURE_LEVEL_10_0 = 0xA000
D3D_FEATURE_LEVEL_10_1 = 0xA100
D3D_FEATURE_LEVEL_11_0 = 0xB000
D3D_FEATURE_LEVEL_11_1 = 0xB100

D3D11_USAGE_DEFAULT = 0
D3D11_USAGE_IMMUTABLE = 1
D3D11_USAGE_DYNAMIC = 2
D3D11_USAGE_STAGING = 3

DXGI_FORMAT_B8G8R8A8_UNORM = 87


class DXGI_SAMPLE_DESC(ctypes.Structure):
    _fields_ = [
        ("Count", wintypes.UINT),
        ("Quality", wintypes.UINT),
    ]


class D3D11_BOX(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.UINT),
        ("top", wintypes.UINT),
        ("front", wintypes.UINT),
        ("right", wintypes.UINT),
        ("bottom", wintypes.UINT),
        ("back", wintypes.UINT),
    ]


class D3D11_TEXTURE2D_DESC(ctypes.Structure):
    _fields_ = [
        ("Width", wintypes.UINT),
        ("Height", wintypes.UINT),
        ("MipLevels", wintypes.UINT),
        ("ArraySize", wintypes.UINT),
        ("Format", wintypes.UINT),
        ("SampleDesc", DXGI_SAMPLE_DESC),
        ("Usage", wintypes.UINT),
        ("BindFlags", wintypes.UINT),
        ("CPUAccessFlags", wintypes.UINT),
        ("MiscFlags", wintypes.UINT),
    ]


class ID3D11DeviceChild(comtypes.IUnknown):
    _iid_ = comtypes.GUID("{1841e5c8-16b0-489b-bcc8-44cfb0d5deae}")
    _methods_ = [
        comtypes.STDMETHOD(None, "GetDevice"),
        comtypes.STDMETHOD(comtypes.HRESULT, "GetPrivateData"),
        comtypes.STDMETHOD(comtypes.HRESULT, "SetPrivateData"),
        comtypes.STDMETHOD(comtypes.HRESULT, "SetPrivateDataInterface"),
    ]


class ID3D11Resource(ID3D11DeviceChild):
    _iid_ = comtypes.GUID("{dc8e63f3-d12b-4952-b47b-5e45026a862d}")
    _methods_ = [
        comtypes.STDMETHOD(None, "GetType"),
        comtypes.STDMETHOD(None, "SetEvictionPriority"),
        comtypes.STDMETHOD(wintypes.UINT, "GetEvictionPriority"),
    ]


class ID3D11Texture2D(ID3D11Resource):
    _iid_ = comtypes.GUID("{6f15aaf2-d208-4e89-9ab4-489535d34f9c}")
    _methods_ = [
        comtypes.STDMETHOD(None, "GetDesc", [ctypes.POINTER(D3D11_TEXTURE2D_DESC)]),
    ]


class ID3D11DeviceContext(ID3D11DeviceChild):
    _iid_ = comtypes.GUID("{c0bfa96c-e089-44fb-8eaf-26f8796190da}")
    _methods_ = [
        comtypes.STDMETHOD(None, "VSSetConstantBuffers"),
        comtypes.STDMETHOD(None, "PSSetShaderResources"),
        comtypes.STDMETHOD(None, "PSSetShader"),
        comtypes.STDMETHOD(None, "PSSetSamplers"),
        comtypes.STDMETHOD(None, "VSSetShader"),
        comtypes.STDMETHOD(None, "DrawIndexed"),
        comtypes.STDMETHOD(None, "Draw"),
        comtypes.STDMETHOD(comtypes.HRESULT, "Map"),
        comtypes.STDMETHOD(None, "Unmap"),
        comtypes.STDMETHOD(None, "PSSetConstantBuffers"),
        comtypes.STDMETHOD(None, "IASetInputLayout"),
        comtypes.STDMETHOD(None, "IASetVertexBuffers"),
        comtypes.STDMETHOD(None, "IASetIndexBuffer"),
        comtypes.STDMETHOD(None, "DrawIndexedInstanced"),
        comtypes.STDMETHOD(None, "DrawInstanced"),
        comtypes.STDMETHOD(None, "GSSetConstantBuffers"),
        comtypes.STDMETHOD(None, "GSSetShader"),
        comtypes.STDMETHOD(None, "IASetPrimitiveTopology"),
        comtypes.STDMETHOD(None, "VSSetShaderResources"),
        comtypes.STDMETHOD(None, "VSSetSamplers"),
        comtypes.STDMETHOD(None, "Begin"),
        comtypes.STDMETHOD(None, "End"),
        comtypes.STDMETHOD(comtypes.HRESULT, "GetData"),
        comtypes.STDMETHOD(None, "SetPredication"),
        comtypes.STDMETHOD(None, "GSSetShaderResources"),
        comtypes.STDMETHOD(None, "GSSetSamplers"),
        comtypes.STDMETHOD(None, "OMSetRenderTargets"),
        comtypes.STDMETHOD(None, "OMSetRenderTargetsAndUnorderedAccessViews"),
        comtypes.STDMETHOD(None, "OMSetBlendState"),
        comtypes.STDMETHOD(None, "OMSetDepthStencilState"),
        comtypes.STDMETHOD(None, "SOSetTargets"),
        comtypes.STDMETHOD(None, "DrawAuto"),
        comtypes.STDMETHOD(None, "DrawIndexedInstancedIndirect"),
        comtypes.STDMETHOD(None, "DrawInstancedIndirect"),
        comtypes.STDMETHOD(None, "Dispatch"),
        comtypes.STDMETHOD(None, "DispatchIndirect"),
        comtypes.STDMETHOD(None, "RSSetState"),
        comtypes.STDMETHOD(None, "RSSetViewports"),
        comtypes.STDMETHOD(None, "RSSetScissorRects"),
        comtypes.STDMETHOD(
            None,
            "CopySubresourceRegion",
            [
                ctypes.POINTER(ID3D11Resource),
                wintypes.UINT,
                wintypes.UINT,
                wintypes.UINT,
                wintypes.UINT,
                ctypes.POINTER(ID3D11Resource),
                wintypes.UINT,
                ctypes.POINTER(D3D11_BOX),
            ],
        ),
        comtypes.STDMETHOD(
            None,
            "CopyResource",
            [ctypes.POINTER(ID3D11Resource), ctypes.POINTER(ID3D11Resource)],
        ),
        comtypes.STDMETHOD(None, "UpdateSubresource"),
        comtypes.STDMETHOD(None, "CopyStructureCount"),
        comtypes.STDMETHOD(None, "ClearRenderTargetView"),
        comtypes.STDMETHOD(None, "ClearUnorderedAccessViewUint"),
        comtypes.STDMETHOD(None, "ClearUnorderedAccessViewFloat"),
        comtypes.STDMETHOD(None, "ClearDepthStencilView"),
        comtypes.STDMETHOD(None, "GenerateMips"),
        comtypes.STDMETHOD(None, "SetResourceMinLOD"),
        comtypes.STDMETHOD(wintypes.FLOAT, "GetResourceMinLOD"),
        comtypes.STDMETHOD(None, "ResolveSubresource"),
        comtypes.STDMETHOD(None, "ExecuteCommandList"),
        comtypes.STDMETHOD(None, "HSSetShaderResources"),
        comtypes.STDMETHOD(None, "HSSetShader"),
        comtypes.STDMETHOD(None, "HSSetSamplers"),
        comtypes.STDMETHOD(None, "HSSetConstantBuffers"),
        comtypes.STDMETHOD(None, "DSSetShaderResources"),
        comtypes.STDMETHOD(None, "DSSetShader"),
        comtypes.STDMETHOD(None, "DSSetSamplers"),
        comtypes.STDMETHOD(None, "DSSetConstantBuffers"),
        comtypes.STDMETHOD(None, "CSSetShaderResources"),
        comtypes.STDMETHOD(None, "CSSetUnorderedAccessViews"),
        comtypes.STDMETHOD(None, "CSSetShader"),
        comtypes.STDMETHOD(None, "CSSetSamplers"),
        comtypes.STDMETHOD(None, "CSSetConstantBuffers"),
        comtypes.STDMETHOD(None, "VSGetConstantBuffers"),
        comtypes.STDMETHOD(None, "PSGetShaderResources"),
        comtypes.STDMETHOD(None, "PSGetShader"),
        comtypes.STDMETHOD(None, "PSGetSamplers"),
        comtypes.STDMETHOD(None, "VSGetShader"),
        comtypes.STDMETHOD(None, "PSGetConstantBuffers"),
        comtypes.STDMETHOD(None, "IAGetInputLayout"),
        comtypes.STDMETHOD(None, "IAGetVertexBuffers"),
        comtypes.STDMETHOD(None, "IAGetIndexBuffer"),
        comtypes.STDMETHOD(None, "GSGetConstantBuffers"),
        comtypes.STDMETHOD(None, "GSGetShader"),
        comtypes.STDMETHOD(None, "IAGetPrimitiveTopology"),
        comtypes.STDMETHOD(None, "VSGetShaderResources"),
        comtypes.STDMETHOD(None, "VSGetSamplers"),
        comtypes.STDMETHOD(None, "GetPredication"),
        comtypes.STDMETHOD(None, "GSGetShaderResources"),
        comtypes.STDMETHOD(None, "GSGetSamplers"),
        comtypes.STDMETHOD(None, "OMGetRenderTargets"),
        comtypes.STDMETHOD(None, "OMGetRenderTargetsAndUnorderedAccessViews"),
        comtypes.STDMETHOD(None, "OMGetBlendState"),
        comtypes.STDMETHOD(None, "OMGetDepthStencilState"),
        comtypes.STDMETHOD(None, "SOGetTargets"),
        comtypes.STDMETHOD(None, "RSGetState"),
        comtypes.STDMETHOD(None, "RSGetViewports"),
        comtypes.STDMETHOD(None, "RSGetScissorRects"),
        comtypes.STDMETHOD(None, "HSGetShaderResources"),
        comtypes.STDMETHOD(None, "HSGetShader"),
        comtypes.STDMETHOD(None, "HSGetSamplers"),
        comtypes.STDMETHOD(None, "HSGetConstantBuffers"),
        comtypes.STDMETHOD(None, "DSGetShaderResources"),
        comtypes.STDMETHOD(None, "DSGetShader"),
        comtypes.STDMETHOD(None, "DSGetSamplers"),
        comtypes.STDMETHOD(None, "DSGetConstantBuffers"),
        comtypes.STDMETHOD(None, "CSGetShaderResources"),
        comtypes.STDMETHOD(None, "CSGetUnorderedAccessViews"),
        comtypes.STDMETHOD(None, "CSGetShader"),
        comtypes.STDMETHOD(None, "CSGetSamplers"),
        comtypes.STDMETHOD(None, "CSGetConstantBuffers"),
        comtypes.STDMETHOD(None, "ClearState"),
        comtypes.STDMETHOD(None, "Flush"),
        comtypes.STDMETHOD(None, "GetType"),
        comtypes.STDMETHOD(wintypes.UINT, "GetContextFlags"),
        comtypes.STDMETHOD(comtypes.HRESULT, "FinishCommandList"),
    ]


class ID3D11Device(comtypes.IUnknown):
    _iid_ = comtypes.GUID("{db6f6ddb-ac77-4e88-8253-819df9bbf140}")
    _methods_ = [
        comtypes.STDMETHOD(comtypes.HRESULT, "CreateBuffer"),
        comtypes.STDMETHOD(comtypes.HRESULT, "CreateTexture1D"),
        comtypes.STDMETHOD(
            comtypes.HRESULT,
            "CreateTexture2D",
            [
                ctypes.POINTER(D3D11_TEXTURE2D_DESC),
                ctypes.POINTER(None),
                ctypes.POINTER(ctypes.POINTER(ID3D11Texture2D)),
            ],
        ),
        comtypes.STDMETHOD(comtypes.HRESULT, "CreateTexture3D"),
        comtypes.STDMETHOD(comtypes.HRESULT, "CreateShaderResourceView"),
        comtypes.STDMETHOD(comtypes.HRESULT, "CreateUnorderedAccessView"),
        comtypes.STDMETHOD(comtypes.HRESULT, "CreateRenderTargetView"),
        comtypes.STDMETHOD(comtypes.HRESULT, "CreateDepthStencilView"),
        comtypes.STDMETHOD(comtypes.HRESULT, "CreateInputLayout"),
        comtypes.STDMETHOD(comtypes.HRESULT, "CreateVertexShader"),
        comtypes.STDMETHOD(comtypes.HRESULT, "CreateGeometryShader"),
        comtypes.STDMETHOD(comtypes.HRESULT, "CreateGeometryShaderWithStreamOutput"),
        comtypes.STDMETHOD(comtypes.HRESULT, "CreatePixelShader"),
        comtypes.STDMETHOD(comtypes.HRESULT, "CreateHullShader"),
        comtypes.STDMETHOD(comtypes.HRESULT, "CreateDomainShader"),
        comtypes.STDMETHOD(comtypes.HRESULT, "CreateComputeShader"),
        comtypes.STDMETHOD(comtypes.HRESULT, "CreateClassLinkage"),
        comtypes.STDMETHOD(comtypes.HRESULT, "CreateBlendState"),
        comtypes.STDMETHOD(comtypes.HRESULT, "CreateDepthStencilState"),
        comtypes.STDMETHOD(comtypes.HRESULT, "CreateRasterizerState"),
        comtypes.STDMETHOD(comtypes.HRESULT, "CreateSamplerState"),
        comtypes.STDMETHOD(comtypes.HRESULT, "CreateQuery"),
        comtypes.STDMETHOD(comtypes.HRESULT, "CreatePredicate"),
        comtypes.STDMETHOD(comtypes.HRESULT, "CreateCounter"),
        comtypes.STDMETHOD(comtypes.HRESULT, "CreateDeferredContext"),
        comtypes.STDMETHOD(comtypes.HRESULT, "OpenSharedResource"),
        comtypes.STDMETHOD(comtypes.HRESULT, "CheckFormatSupport"),
        comtypes.STDMETHOD(comtypes.HRESULT, "CheckMultisampleQualityLevels"),
        comtypes.STDMETHOD(comtypes.HRESULT, "CheckCounterInfo"),
        comtypes.STDMETHOD(comtypes.HRESULT, "CheckCounter"),
        comtypes.STDMETHOD(comtypes.HRESULT, "CheckFeatureSupport"),
        comtypes.STDMETHOD(comtypes.HRESULT, "GetPrivateData"),
        comtypes.STDMETHOD(comtypes.HRESULT, "SetPrivateData"),
        comtypes.STDMETHOD(comtypes.HRESULT, "SetPrivateDataInterface"),
        comtypes.STDMETHOD(ctypes.c_int32, "GetFeatureLevel"),
        comtypes.STDMETHOD(ctypes.c_uint, "GetCreationFlags"),
        comtypes.STDMETHOD(comtypes.HRESULT, "GetDeviceRemovedReason"),
        comtypes.STDMETHOD(
            None,
            "GetImmediateContext",
            [ctypes.POINTER(ctypes.POINTER(ID3D11DeviceContext))],
        ),
        comtypes.STDMETHOD(comtypes.HRESULT, "SetExceptionMode"),
        comtypes.STDMETHOD(ctypes.c_uint, "GetExceptionMode"),
    ]
