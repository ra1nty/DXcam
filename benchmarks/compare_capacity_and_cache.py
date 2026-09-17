"""Synthetic before/after benchmarks for capacity waiting and cached grab_into.

No GPU device, desktop capture, renderer, or image artifact is created. Capacity
uses real FrameBuffer/CaptureWorker objects with fake stages; cache reuse uses
real DXCamera._grab_into with a no-new-frame acquisition stub. These are bounded
CPU microbenchmarks, not end-to-end capture or display latency measurements.

After freezing both source trees, run with the shared virtual environment:
    .venv/Scripts/python.exe -I benchmarks/compare_capacity_and_cache.py \
        --baseline-root .test/perf_baseline

Defaults: three randomized blocks, one one-second saturated-worker case plus
six cache cases per source (320x180, 1920x1080, 3840x2160; contiguous/row-strided).
Each trial gets a fresh process. Timing and allocation tracing are separate.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import gc
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import random
import subprocess
import sys
from threading import Event, Lock
import time
import tracemalloc

ROOT = Path(__file__).resolve().parents[1]
SIZES = ((320, 180), (1920, 1080), (3840, 2160))
LAYOUTS = ("contiguous", "row_strided")


def runtime_fingerprint(root):
    files = {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted((root / "dxcam").rglob("*"))
        if path.is_file()
        and path.suffix.lower() in {".py", ".pyx", ".pyd"}
        and "__pycache__" not in path.parts
    }
    if not files or "dxcam/__init__.py" not in files:
        raise ValueError(f"No dxcam source tree at {root}")
    digest = hashlib.sha256(
        json.dumps(files, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {"sha256": digest, "files_sha256": files}


def source_metadata(root):
    provenance = root / "provenance.json"
    snapshot = json.loads(provenance.read_text()) if provenance.exists() else None
    head = None
    if (root / ".git").exists():
        head = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True
        ).strip()
    elif isinstance(snapshot, dict):
        head = snapshot.get("git_head")
    return {
        "root": str(root),
        "git_head": head,
        "snapshot_metadata": snapshot,
        "runtime": runtime_fingerprint(root),
    }


class FakeStage:
    def __init__(self):
        self.releases = 0

    def release(self):
        self.releases += 1


def capacity_trial(args):
    from dxcam.runtime.capture_worker import CaptureWorker
    from dxcam.runtime.frame_buffer import FrameBuffer

    frame_buffer = FrameBuffer()
    stages = [FakeStage() for _ in range(3)]
    frame_buffer.replace_slots(stages, frame_width=2, frame_height=2, rotation_angle=0)
    leases = []
    for ticks in range(1, 4):
        slot = frame_buffer.reserve_write_slot()
        if slot is None or not frame_buffer.commit_write(
            slot, frame_ticks=ticks, frame_width=2, frame_height=2, rotation_angle=0
        ):
            raise RuntimeError("Unable to prepare saturated buffer.")
        if ticks < 3:
            leases.append(frame_buffer.lease_latest_slot())
    if frame_buffer.reserve_write_slot() is not None or any(
        lease is None for lease in leases
    ):
        raise RuntimeError("The two old leases must exclude all writable slots.")

    capture_calls = 0

    def capture(_region, _stage):
        nonlocal capture_calls
        capture_calls += 1
        return True, 3 + capture_calls, 2, 2, 0

    worker = CaptureWorker(
        frame_buffer, Lock(), capture, lambda: (0, 0, 2, 2), target_fps=0
    )

    def stop_and_join():
        started = time.perf_counter()
        worker.stop()
        if not worker.join(timeout=2):
            raise RuntimeError("Synthetic capture worker did not stop within 2 s.")
        elapsed = time.perf_counter() - started
        error = worker.consume_error()
        if error is not None:
            raise RuntimeError(f"Synthetic capture worker failed: {error}") from error
        return elapsed

    try:
        worker.start()
        Event().wait(args.warmup)
        if not worker.is_running():
            raise RuntimeError("Synthetic capture worker stopped before measurement.")
        started = time.perf_counter()
        cpu_started = time.process_time()
        Event().wait(args.capacity_seconds)
        cpu_seconds = time.process_time() - cpu_started
        elapsed = time.perf_counter() - started
        with worker.frame_condition:
            if capture_calls != 0 or frame_buffer.frame_count != 3:
                raise RuntimeError(
                    "A saturated producer unexpectedly acquired a frame."
                )
        saturated_stop = stop_and_join()

        # Restart still saturated, then exercise the same lease-release/notify
        # protocol as the camera. Wake correctness is outside the CPU window.
        worker.start()
        Event().wait(args.warmup)
        with worker.frame_condition:
            released_at = time.perf_counter()
            freed_capacity = frame_buffer.release_lease(leases[0])
            capacity_condition = getattr(worker, "capacity_condition", None)
            if capacity_condition is None:
                # Frozen baseline has no separate capacity condition and its
                # release_lease returns None. Its producer polls for capacity.
                worker.frame_condition.notify_all()
            elif freed_capacity:
                # Both conditions share worker.lock, already held here.
                capacity_condition.notify_all()
            else:
                raise RuntimeError("Releasing an old lease did not report capacity.")
            resumed = worker.frame_condition.wait_for(
                lambda: frame_buffer.frame_count > 3 or worker.stopped, timeout=2
            )
            resume_seconds = time.perf_counter() - released_at
            if not resumed or frame_buffer.frame_count <= 3:
                raise RuntimeError(
                    "Releasing a retained lease did not resume publication."
                )
            # Prevent another acquisition once the assertion has observed a commit.
            worker.stop_event.set()
            worker.frame_condition.notify_all()
            if capacity_condition is not None:
                capacity_condition.notify_all()
        resumed_stop = stop_and_join()
        result = {
            "target_fps": 0,
            "saturation_seconds_requested": args.capacity_seconds,
            "elapsed_s": elapsed,
            "cpu_seconds": cpu_seconds,
            "process_cpu_percent_one_core": cpu_seconds / elapsed * 100,
            "capture_calls_while_saturated": 0,
            "capture_calls_after_release": capture_calls,
            "release_to_observed_publication_s": resume_seconds,
            "saturated_stop_seconds": saturated_stop,
            "resumed_stop_seconds": resumed_stop,
        }
    finally:
        worker.stop()
        joined = worker.join(timeout=2)
        if joined:
            with worker.frame_condition:
                for lease in leases:
                    frame_buffer.release_lease(lease)
                frame_buffer.clear()
        if not joined:
            raise RuntimeError("Synthetic worker remained alive during cleanup.")
    if [stage.releases for stage in stages] != [1, 1, 1]:
        raise RuntimeError("Fake staging resources were not released exactly once.")
    result["verified"] = True
    return result


def cache_trial(args):
    import numpy as np
    from dxcam.dxcam import DXCamera

    width, height = args.width, args.height
    cached = np.empty((height, width, 3), dtype=np.uint8)
    cached[:, :, 0] = np.arange(width, dtype=np.uint8)
    cached[:, :, 1] = np.arange(height, dtype=np.uint8)[:, None]
    cached[:, :, 2] = 173
    sentinel = 219
    backing = np.full(
        (height * (2 if args.layout == "row_strided" else 1), width, 3),
        sentinel,
        dtype=np.uint8,
    )
    dst = backing[::2] if args.layout == "row_strided" else backing
    region = (0, 0, width, height)
    camera = DXCamera.__new__(DXCamera)
    camera._is_released = True  # No hardware resources exist for destructor cleanup.
    camera._stagesurf = object()
    camera.channel_size = 3
    camera.rotation_angle = 0
    camera._capture_to_stage = lambda *_args, **_kwargs: (False, 0, 0, 0, 0)
    camera._set_cached_grab_frame(region, cached)

    def copy_cached():
        if not camera._grab_into(region, dst=dst, new_frame_only=False):
            raise RuntimeError("Cached grab_into unexpectedly returned no frame.")

    def verify():
        if not np.array_equal(dst, cached):
            raise RuntimeError("Cached grab_into output differs from cached pixels.")
        if args.layout == "row_strided" and not np.all(backing[1::2] == sentinel):
            raise RuntimeError("Cached grab_into overwrote row padding.")
        if (
            not np.all(cached[:, :, 0] == np.arange(width, dtype=np.uint8))
            or not np.all(cached[:, :, 1] == np.arange(height, dtype=np.uint8)[:, None])
            or not np.all(cached[:, :, 2] == 173)
        ):
            raise RuntimeError("Cached source pixels were modified.")

    copy_cached()
    verify()
    for _ in range(8):
        copy_cached()
    iterations = max(16, math.ceil(args.copy_mib * 2**20 / cached.nbytes))
    gc.collect()
    gc_enabled = gc.isenabled()
    gc.disable()
    try:
        started = time.perf_counter()
        cpu_started = time.process_time()
        for _ in range(iterations):
            copy_cached()
        cpu_seconds = time.process_time() - cpu_started
        elapsed = time.perf_counter() - started
    finally:
        if gc_enabled:
            gc.enable()
    verify()

    # NumPy registers array storage with tracemalloc. Check that assumption in
    # this exact environment, then trace one call independently of timed work.
    tracemalloc.start()
    try:
        probe = np.empty_like(cached)
        numpy_probe_peak = tracemalloc.get_traced_memory()[1]
        del probe
        if numpy_probe_peak < cached.nbytes:
            raise RuntimeError("tracemalloc did not observe NumPy array allocation.")
        tracemalloc.reset_peak()
        allocation_before = tracemalloc.get_traced_memory()[0]
        copy_cached()
        allocation_current, allocation_peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    verify()
    return {
        "width": width,
        "height": height,
        "layout": args.layout,
        "frame_bytes": cached.nbytes,
        "destination_strides": list(dst.strides),
        "iterations": iterations,
        "logical_output_bytes": iterations * cached.nbytes,
        "elapsed_s": elapsed,
        "cpu_seconds": cpu_seconds,
        "wall_us_per_call": elapsed / iterations * 1e6,
        "cpu_us_per_call": cpu_seconds / iterations * 1e6,
        "logical_output_gib_per_s": iterations * cached.nbytes / 2**30 / elapsed,
        "single_call_traced_peak_extra_bytes": allocation_peak - allocation_before,
        "single_call_traced_retained_extra_bytes": allocation_current
        - allocation_before,
        "numpy_probe_peak_bytes": numpy_probe_peak,
        "numpy_version": np.__version__,
        "verified": True,
    }


def worker(args):
    root = Path(args.source_root).resolve()
    before = runtime_fingerprint(root)
    sys.path.insert(0, str(root))
    import dxcam

    imported = Path(dxcam.__file__).resolve()
    if imported.parent != root / "dxcam":
        raise RuntimeError(f"Worker imported the wrong dxcam tree: {imported}")
    result = capacity_trial(args) if args.case == "capacity" else cache_trial(args)
    after = runtime_fingerprint(root)
    result.update(
        case=args.case,
        source_root=str(root),
        dxcam_file=str(imported),
        runtime_start=before,
        runtime_end=after,
        runtime_unchanged=before == after,
        python=platform.python_version(),
    )
    if before != after:
        result["error"] = "Worker runtime changed during the trial."
    print(json.dumps(result), flush=True)


def trial_key(trial):
    return tuple(
        trial.get(key)
        for key in ("repeat", "variant", "case", "width", "height", "layout")
    )


def compare(args):
    roots = {
        "baseline": Path(args.baseline_root).resolve(),
        "candidate": Path(args.candidate_root).resolve(),
    }
    if roots["baseline"] == roots["candidate"]:
        raise ValueError("Baseline and candidate must use different source roots.")
    sources = {name: source_metadata(root) for name, root in roots.items()}
    configurations = [{"case": "capacity"}] + [
        {"case": "cache", "width": width, "height": height, "layout": layout}
        for width, height in SIZES
        for layout in LAYOUTS
    ]
    planned = [
        {"repeat": repeat, "variant": variant, **config}
        for repeat in range(1, args.repeats + 1)
        for variant in roots
        for config in configurations
    ]
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    helper_hash = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    payload = {
        "experiment": "synthetic_capacity_and_cached_copy",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "arguments": vars(args),
        "platform": platform.platform(),
        "cpu_logical_count": os.cpu_count(),
        "sources": sources,
        "helper_sha256_start": helper_hash,
        "planned_trials": planned,
        "expected_trials": len(planned),
        "trials": [],
        "limitations": [
            "Synthetic CPU microbenchmarks: no GPU, acquisition, map, conversion or desktop latency is measured.",
            "Capacity holds two old leases and a third latest slot; zero-FPS worker saturation CPU excludes setup, resume and stop checks.",
            "Lease release notifies the capacity condition when release_lease reports capacity; baseline has only a frame condition. This exercises the wait protocol, not the public camera release path.",
            "CPU is process time; 100% equals one logical core. Main-thread waiting and wake overhead are included.",
            "Windows process CPU-time granularity can quantize short cache trials; wall time is the primary cache timing metric.",
            "Cache timing includes the real _grab_into cached path and a Python acquisition stub; GC is disabled equally during timing.",
            "Logical output bandwidth counts destination bytes once, not total memory traffic or hardware bandwidth.",
            "Tracemalloc peak is one separate call, includes NumPy-traced array storage, and is not RSS, GPU memory or total process memory.",
            "Correctness checks and source hashes run outside the timed and traced operations. Runtime .py/.pyx/.pyd files are fingerprinted.",
        ],
    }
    rng = random.Random(args.seed)

    def save():
        after = {name: runtime_fingerprint(root) for name, root in roots.items()}
        payload["runtime_end"] = after
        helper_after = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        payload["helper_sha256_end"] = helper_after
        unchanged = helper_after == helper_hash and all(
            after[name] == source["runtime"] for name, source in sources.items()
        )
        for trial in payload["trials"]:
            if "error" in trial:
                if trial.get("runtime_unchanged") is False:
                    unchanged = False
                continue
            expected = sources[trial["variant"]]["runtime"]
            if (
                trial.get("runtime_start") != expected
                or trial.get("runtime_end") != expected
                or trial.get("runtime_unchanged") is not True
                or trial.get("verified") is not True
            ):
                trial["error"] = "Worker provenance or correctness checks failed."
                unchanged = False
        payload["provenance_unchanged"] = (
            payload.get("provenance_unchanged", True) and unchanged
        )
        if not unchanged and payload["trials"]:
            payload["trials"][-1].setdefault(
                "error", "Sources/helper changed during run."
            )
        payload["completed_trials"] = sum("error" not in t for t in payload["trials"])
        payload["coverage_complete"] = Counter(map(trial_key, planned)) == Counter(
            map(trial_key, payload["trials"])
        )
        output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    try:
        for repeat in range(1, args.repeats + 1):
            block = [dict(trial) for trial in planned if trial["repeat"] == repeat]
            rng.shuffle(block)
            for trial in block:
                trial["order"] = len(payload["trials"]) + 1
                command = [
                    sys.executable,
                    "-I",
                    str(Path(__file__).resolve()),
                    "--worker",
                    "--source-root",
                    str(roots[trial["variant"]]),
                    "--case",
                    trial["case"],
                    "--capacity-seconds",
                    str(args.capacity_seconds),
                    "--warmup",
                    str(args.warmup),
                    "--copy-mib",
                    str(args.copy_mib),
                ]
                if trial["case"] == "cache":
                    for key in ("width", "height", "layout"):
                        command.extend((f"--{key}", str(trial[key])))
                try:
                    completed = subprocess.run(
                        command,
                        cwd=ROOT,
                        capture_output=True,
                        text=True,
                        timeout=max(60, args.capacity_seconds + 2 * args.warmup + 15),
                    )
                    if completed.returncode:
                        raise RuntimeError(
                            completed.stderr[-6000:] or completed.stdout[-6000:]
                        )
                    trial.update(json.loads(completed.stdout.strip().splitlines()[-1]))
                except (
                    OSError,
                    subprocess.SubprocessError,
                    RuntimeError,
                    ValueError,
                    IndexError,
                ) as exc:
                    trial["error"] = str(exc)
                payload["trials"].append(trial)
                save()
                print(
                    json.dumps(
                        {
                            key: value
                            for key, value in trial.items()
                            if not key.startswith("runtime_")
                        }
                    ),
                    flush=True,
                )
                if not payload["provenance_unchanged"]:
                    raise RuntimeError(
                        "Source/helper provenance changed; comparison aborted."
                    )
    finally:
        save()
    print(f"Saved synthetic benchmark: {output}", flush=True)
    return (
        0
        if (
            payload["coverage_complete"]
            and payload["provenance_unchanged"]
            and payload["completed_trials"] == payload["expected_trials"]
        )
        else 1
    )


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-root")
    parser.add_argument("--candidate-root", default=str(ROOT))
    parser.add_argument(
        "--output", default=str(ROOT / "benchmarks/results/capacity_and_cache.json")
    )
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--seed", type=int, default=20260918)
    parser.add_argument("--capacity-seconds", type=float, default=1)
    parser.add_argument("--warmup", type=float, default=0.1)
    parser.add_argument("--copy-mib", type=float, default=1024)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--source-root", help=argparse.SUPPRESS)
    parser.add_argument("--case", choices=("capacity", "cache"), help=argparse.SUPPRESS)
    parser.add_argument("--width", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--height", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--layout", choices=LAYOUTS, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.repeats < 1 or any(
        not math.isfinite(value) or value <= 0
        for value in (args.capacity_seconds, args.warmup, args.copy_mib)
    ):
        parser.error("Repeats, durations and copy budget must be positive and finite.")
    if args.worker:
        if args.source_root is None or args.case is None:
            parser.error("Workers require --source-root and --case.")
        if args.case == "cache" and (
            (args.width, args.height) not in SIZES or args.layout is None
        ):
            parser.error(
                "Cache workers require a supported size and destination layout."
            )
    elif args.baseline_root is None:
        parser.error("A comparison requires --baseline-root.")
    return args


if __name__ == "__main__":
    arguments = parse_args()
    if arguments.worker:
        worker(arguments)
    else:
        sys.exit(compare(arguments))
