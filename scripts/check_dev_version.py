#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import re


def read_project_version(pyproject: Path) -> str:
    in_project = False
    version_pattern = re.compile(r'^version\s*=\s*"([^"]+)"\s*$')

    for raw_line in pyproject.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            in_project = line == "[project]"
            continue
        if in_project:
            match = version_pattern.match(line)
            if match:
                return match.group(1)

    raise RuntimeError("Could not find [project].version in pyproject.toml")


def main() -> int:
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    version = read_project_version(pyproject)

    if "+" in version:
        print(
            f"Invalid dev version '{version}': local version labels ('+') are not "
            "accepted by PyPI."
        )
        return 1

    if re.search(r"\.dev\d+", version) is None:
        print(
            f"Invalid dev version '{version}': expected a PEP 440 dev release "
            "such as '1.1.0.dev1'."
        )
        return 1

    print(f"Dev version check passed: {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
