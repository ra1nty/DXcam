#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path
import re


def normalize_tag(tag: str) -> str:
    return tag[1:] if tag.startswith("v") else tag


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
    parser = argparse.ArgumentParser(
        description="Ensure release tag matches project.version in pyproject.toml"
    )
    parser.add_argument("tag", help="Git tag, e.g. v0.0.5")
    args = parser.parse_args()

    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    version = read_project_version(pyproject)
    tag_version = normalize_tag(args.tag.strip())

    if tag_version != version:
        print(
            "Version mismatch: "
            f"tag '{args.tag}' expects project.version '{tag_version}', "
            f"but pyproject.toml has '{version}'."
        )
        return 1

    print(f"Version check passed: {args.tag} matches project.version {version}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
