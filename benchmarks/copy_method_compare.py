from __future__ import annotations

import argparse
import statistics
import subprocess
import sys


def run_case(frames: int, region: tuple[int, int, int, int] | None) -> float:
    if region is None:
        region_literal = "None"
    else:
        region_literal = str(region)

    code = f"""
import time
import dxcam
cam = dxcam.create(output_color='RGB')
region = {region_literal}
captured = 0
start = time.perf_counter()
while captured < {frames}:
    frame = cam.grab(region=region)
    if frame is not None:
        captured += 1
elapsed = time.perf_counter() - start
cam.release()
print(captured / elapsed)
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
    )
    return float(result.stdout.strip().splitlines()[-1])


def summarize(label: str, values: list[float]) -> None:
    mean_fps = statistics.mean(values)
    median_fps = statistics.median(values)
    print(f"{label}")
    print(f"  mean fps: {mean_fps:.2f}  median fps: {median_fps:.2f}")
    print(f"  runs={', '.join(f'{v:.2f}' for v in values)}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Benchmark CopySubresourceRegion capture path performance."
    )
    parser.add_argument("--frames", type=int, default=300)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--region", type=str, default="100,100,700,500")
    args = parser.parse_args()

    if args.region.lower() in {"none", "full"}:
        region = None
    else:
        left, top, right, bottom = (int(x) for x in args.region.split(","))
        region = (left, top, right, bottom)

    full_values: list[float] = []
    region_values: list[float] = []

    print(
        f"Running benchmark: frames={args.frames} runs={args.runs} region={region if region is not None else 'full-screen'}"
    )

    for _ in range(args.runs):
        full_values.append(run_case(args.frames, None))
        if region is not None:
            region_values.append(run_case(args.frames, region))

    summarize("Full Screen", full_values)
    if region is not None:
        summarize(f"Region {region}", region_values)


if __name__ == "__main__":
    main()
