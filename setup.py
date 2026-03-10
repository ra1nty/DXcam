from __future__ import annotations

import os
import sys

from setuptools import Extension, setup


def _cython_compile_args() -> list[str]:
    if sys.platform.startswith("win"):
        return ["/O2", "/fp:fast", "/openmp"]
    return ["-O3", "-ffast-math", "-fopenmp"]


def _cython_link_args() -> list[str]:
    if sys.platform.startswith("win"):
        return []
    return ["-fopenmp"]


def _build_optional_cython_extensions() -> list[Extension]:
    if os.environ.get("DXCAM_BUILD_CYTHON", "0") != "1":
        return []

    try:
        from Cython.Build import cythonize
        import numpy as np
    except Exception as exc:  # pragma: no cover - depends on build env
        raise RuntimeError(
            "DXCAM_BUILD_CYTHON=1 requires Cython and NumPy to be available "
            "in the build environment."
        ) from exc

    extensions = [
        Extension(
            "dxcam.processor._numpy_kernels",
            sources=["dxcam/processor/_numpy_kernels.pyx"],
            include_dirs=[np.get_include()],
            extra_compile_args=_cython_compile_args(),
            extra_link_args=_cython_link_args(),
        )
    ]
    return cythonize(extensions, language_level="3")


setup(ext_modules=_build_optional_cython_extensions())
