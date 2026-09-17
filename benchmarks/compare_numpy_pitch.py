"""Compare isolated NumPy processor readout with two explicit source trees.

Build each tree's extensions with the same toolchain first. No desktop capture
occurs. Each repeat imports one tree in a fresh process; case and version order
are randomized. Timing and allocation tracing are separate measurements.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import gc
import hashlib
import json
import os
from pathlib import Path
import platform
import random
import subprocess
import sys
import time
import tracemalloc
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
LAYOUTS = {
    "roi_padded": (320, 180, 16, 0, None),
    "hd_padded": (1920, 1080, 16, 0, None),
    "4k_padded": (3840, 2160, 16, 0, None),
    "hd_crop": (2048, 1152, 0, 0, (64, 36, 1984, 1116)),
    "hd_packed": (1920, 1080, 0, 0, None),
    "hd_rot90": (1920, 1080, 16, 90, None),
}


def fingerprint(root):
    files = sorted(
        path
        for path in (root / "dxcam").rglob("*")
        if path.suffix in (".py", ".pyx", ".pyd")
    )
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in files
    }


def cases(args):
    selected = [
        (layout, color, api)
        for layout in args.layouts
        for color in args.colors
        for api in args.apis
    ]
    if args.controls:
        selected += [("hd_padded", color, "into_strided") for color in args.colors]
        selected += [("hd_padded", "BGRA", api) for api in args.apis]
    return selected


def measure_case(spec, args, np, Processor):
    layout, color, api = spec
    width, height, padding, rotation, region = LAYOUTS[layout]
    region = region or (0, 0, width, height)
    rng = np.random.default_rng(1729)
    logical = rng.integers(0, 256, size=(height, width, 4), dtype=np.uint8)
    native = np.rot90(logical, k=rotation // 90)
    source = np.full((native.shape[0], native.shape[1] + padding, 4), 239, np.uint8)
    source[:, : native.shape[1]] = native
    source_hash = hashlib.sha256(source).hexdigest()
    rect = SimpleNamespace(Pitch=source.strides[0], pBits=source.ctypes.data)
    left, top, right, bottom = region
    pixels = logical[top:bottom, left:right]
    channels = {"BGR": 3, "RGB": 3, "RGBA": 4, "BGRA": 4, "GRAY": 1}[color]
    if color == "GRAY":
        weighted = pixels[..., :3].astype(np.uint32) @ np.array(
            [3735, 19235, 9798], np.uint32
        )
        expected = ((weighted + 16384) >> 15).astype(np.uint8)[..., None]
    else:
        order = {
            "BGR": [0, 1, 2],
            "RGB": [2, 1, 0],
            "RGBA": [2, 1, 0, 3],
            "BGRA": [0, 1, 2, 3],
        }[color]
        expected = pixels[..., order]
    out_h, out_w = pixels.shape[:2]
    storage = np.full(
        (out_h * (2 if api == "into_strided" else 1), out_w, channels), 197, np.uint8
    )
    dst = storage[::2] if api == "into_strided" else storage
    processor = Processor(backend="numpy", output_color=color)

    def call():
        if api == "process":
            return processor.process(rect, width, height, region, rotation)
        processor.process_into(rect, width, height, region, rotation, dst)
        return dst

    for _ in range(5):
        np.testing.assert_array_equal(call(), expected)
    batch_iterations = max(
        64, min(1024, int(args.copy_mib * 1024 * 1024 / (out_h * out_w * 4)))
    )
    iterations = 0
    wall_ns = 0
    was_enabled = gc.isenabled()
    gc.disable()
    try:
        cpu_start = time.process_time_ns()
        wall_start = time.perf_counter_ns()
        while wall_ns < args.seconds * 1e9:
            for _ in range(batch_iterations):
                call()
            iterations += batch_iterations
            wall_ns = time.perf_counter_ns() - wall_start
        cpu_ns = time.process_time_ns() - cpu_start
    finally:
        if was_enabled:
            gc.enable()
    np.testing.assert_array_equal(call(), expected)
    tracemalloc.start()
    before, _ = tracemalloc.get_traced_memory()
    tracemalloc.reset_peak()
    result = call()
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    np.testing.assert_array_equal(result, expected)
    assert hashlib.sha256(source).hexdigest() == source_hash
    if api == "into_strided":
        assert np.all(storage[1::2] == 197)
    return {
        "layout": layout,
        "color": color,
        "api": api,
        "output_shape": list(expected.shape),
        "pitch_bytes": rect.Pitch,
        "iterations": iterations,
        "wall_seconds": wall_ns / 1e9,
        "cpu_seconds": cpu_ns / 1e9,
        "wall_us": wall_ns / iterations / 1000,
        "cpu_us": cpu_ns / iterations / 1000,
        "extra_peak_bytes": peak - before,
        "retained_bytes": current - before,
        "verified_pixels_source_padding": True,
    }


def worker(args):
    root = Path(args.source_root).resolve()
    sys.path.insert(0, str(root))
    import dxcam
    import numpy as np
    from dxcam.processor import Processor, _numpy_kernels

    assert Path(dxcam.__file__).resolve().parents[1] == root
    if (root / "provenance.json").exists():
        source_commit = json.loads(
            (root / "provenance.json").read_text(encoding="utf-8")
        )["git_head"]
    elif (root / ".git").exists():
        source_commit = subprocess.check_output(
            ["git", "-C", str(root), "rev-parse", "HEAD"], text=True
        ).strip()
    else:
        source_commit = None
    before = fingerprint(root)
    tracemalloc.start()
    allocation = np.empty(1024 * 1024, np.uint8)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert peak >= allocation.nbytes, (
        "NumPy array storage is absent from allocation tracing"
    )
    del allocation
    selected = cases(args)
    random.Random(args.seed).shuffle(selected)
    results = [measure_case(spec, args, np, Processor) for spec in selected]
    assert fingerprint(root) == before, "Source or binary changed during the trial"
    print(
        json.dumps(
            {
                "source_root": str(root),
                "source_commit": source_commit,
                "numpy_extension": str(Path(_numpy_kernels.__file__).resolve()),
                "fingerprints": before,
                "python": platform.python_version(),
                "numpy": np.__version__,
                "platform": platform.platform(),
                "logical_processors": os.cpu_count(),
                "environment": {
                    key: value
                    for key, value in os.environ.items()
                    if key.startswith(("OMP_", "DXCAM_NUMPY_"))
                },
                "parallel_threshold": _numpy_kernels.get_parallel_pixels_threshold(),
                "results": results,
            }
        ),
        flush=True,
    )


def main(args):
    roots = {
        "baseline": Path(args.baseline_root).resolve(),
        "candidate": Path(args.candidate_root).resolve(),
    }
    assert roots["baseline"] != roots["candidate"]
    fingerprints = {name: fingerprint(root) for name, root in roots.items()}
    payload = {
        "status": "running",
        "expected_trials": args.repeats * len(roots),
        "expected_cases_per_trial": len(cases(args)),
        "completed_trials": 0,
        "provenance_verified": False,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "helper_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "method": "Synthetic mapped-memory readout; no GPU capture or end-to-end latency measurement.",
        "config": {
            "repeats": args.repeats,
            "copy_mib": args.copy_mib,
            "minimum_seconds": args.seconds,
            "threads": args.threads,
            "seed": args.seed,
            "cases": cases(args),
        },
        "trials": [],
    }
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    order = random.Random(args.seed)
    try:
        for repeat in range(args.repeats):
            variants = list(roots)
            order.shuffle(variants)
            for variant in variants:
                command = [
                    sys.executable,
                    "-I",
                    str(Path(__file__).resolve()),
                    "--worker",
                    "--source-root",
                    str(roots[variant]),
                    "--seed",
                    str(args.seed + repeat),
                    "--copy-mib",
                    str(args.copy_mib),
                    "--seconds",
                    str(args.seconds),
                    "--layouts",
                    *args.layouts,
                    "--colors",
                    *args.colors,
                    "--apis",
                    *args.apis,
                ]
                if args.controls:
                    command.append("--controls")
                env = dict(os.environ)
                if args.threads is not None:
                    env["OMP_NUM_THREADS"] = str(args.threads)
                print(f"Repeat {repeat + 1}/{args.repeats}: {variant}", flush=True)
                completed = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    env=env,
                    timeout=max(180, args.seconds * len(cases(args)) * 4),
                    check=True,
                )
                trial = json.loads(completed.stdout)
                assert trial["fingerprints"] == fingerprints[variant], (
                    "Runtime changed between trials"
                )
                expected = sorted(tuple(item) for item in cases(args))
                actual = sorted(
                    (item["layout"], item["color"], item["api"])
                    for item in trial["results"]
                )
                assert actual == expected
                payload["trials"].append(
                    {"variant": variant, "repeat": repeat, **trial}
                )
                payload["completed_trials"] = len(payload["trials"])
                output.write_text(
                    json.dumps(payload, indent=2) + "\n", encoding="utf-8"
                )
        assert all(
            fingerprint(root) == fingerprints[name] for name, root in roots.items()
        )
        assert (
            hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
            == payload["helper_sha256"]
        )
        payload["provenance_verified"] = True
        payload["status"] = "complete"
    except BaseException as error:
        payload["status"] = "failed"
        payload["error"] = str(error)
        if isinstance(error, subprocess.CalledProcessError):
            payload["worker_stderr"] = error.stderr[-4000:]
        raise
    finally:
        payload["finished_utc"] = datetime.now(timezone.utc).isoformat()
        output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"Saved {len(payload['trials'])} trials to {output}", flush=True)


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-root")
    parser.add_argument("--candidate-root", default=str(ROOT))
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--copy-mib", type=int, default=128)
    parser.add_argument(
        "--seconds",
        type=float,
        default=0.3,
        help="Minimum wall time per case; each version may complete a different call count.",
    )
    parser.add_argument(
        "--threads",
        type=int,
        help="Optional OpenMP thread count; default preserves the environment.",
    )
    parser.add_argument("--seed", type=int, default=1729)
    parser.add_argument("--layouts", nargs="+", choices=LAYOUTS, default=list(LAYOUTS))
    parser.add_argument(
        "--colors",
        nargs="+",
        choices=("BGR", "RGB", "RGBA", "GRAY"),
        default=["BGR", "RGB", "RGBA", "GRAY"],
    )
    parser.add_argument(
        "--apis", nargs="+", choices=("process", "into"), default=["process", "into"]
    )
    parser.add_argument(
        "--controls",
        action="store_true",
        help="Also measure strided destination and BGRA controls.",
    )
    parser.add_argument(
        "--output", default=str(ROOT / "benchmarks/results/numpy_pitch.json")
    )
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--source-root", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if (
        args.repeats < 1
        or args.copy_mib < 1
        or not 0.1 <= args.seconds <= 10
        or (args.threads is not None and args.threads < 1)
    ):
        parser.error(
            "repeats, copy-mib and threads must be positive; seconds must be 0.1..10"
        )
    if args.worker and not args.source_root:
        parser.error("worker requires source-root")
    if not args.worker and not args.baseline_root:
        parser.error("baseline-root is required")
    return args


if __name__ == "__main__":
    arguments = parse_args()
    worker(arguments) if arguments.worker else main(arguments)
