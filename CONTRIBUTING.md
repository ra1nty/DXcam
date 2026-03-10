# Contributing to DXcam

Thanks for contributing!

## Development Setup
Create a local environment and install development dependencies:

```bash
uv venv --python 3.11 .venv
uv sync --dev
```

Optional extras:

```bash
# OpenCV processor backend support
uv sync --extra cv2

# WinRT capture backend support
uv sync --extra winrt

# Optional Cython tooling
uv sync --extra cython
```

To build local Cython kernels during editable install:

```bash
set DXCAM_BUILD_CYTHON=1
uv pip install -e .[cython] --no-build-isolation
```

## Quality Checks
Run static checks before opening a PR:

```bash
uv run ruff check dxcam
uv run ty check dxcam
```

Run tests:

```bash
uv run pytest -q
```

## API Docs (pdoc)
Build the local API documentation site:

```bash
uv run pdoc -d google -o site dxcam dxcam.dxcam dxcam.types
```

Open `site/index.html` locally to review rendered docs.
