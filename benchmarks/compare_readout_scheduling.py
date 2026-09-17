"""Compare normal readout with a device guard held through CPU conversion.

Experimental monkeypatches apply only inside isolated worker processes. No
production files are modified. The existing acquisition-wait worker supplies
the same marker, age, CPU, memory, and stop measurements for both modes.

Default: animated DXGI/BGR/cv2, target FPS 0/120, requested waits 0/10 ms,
baseline/hold_guard, three five-second trials each (24 trials total).

Run only after other capture workloads and runtime edits have stopped:
    .venv/Scripts/python.exe -I benchmarks/compare_readout_scheduling.py

Keep modes separate when summarizing results: the acquisition-wait summary
helper does not group by this experiment's readout_mode field.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager, redirect_stdout
from datetime import datetime, timezone
import hashlib
import io
import json
import math
import os
from pathlib import Path
import platform
import random
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "benchmarks"))
import compare_acquisition_wait as acquisition  # noqa: E402

MODES = ("baseline", "hold_guard")


def runtime_fingerprint():
    """Include compiled Cython code as well as its Python/Cython sources."""
    files = {
        path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted((ROOT / "dxcam").rglob("*"))
        if path.is_file()
        and path.suffix.lower() in {".py", ".pyx", ".pyd"}
        and "__pycache__" not in path.parts
    }
    digest = hashlib.sha256(
        json.dumps(files, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {"sha256": digest, "files_sha256": files}


@contextmanager
def guarded_mapped(stage):
    """Keep processor -> stage -> device ordering; retain existing Map guards."""
    with stage._map_lock:
        if stage._device is None:
            raise RuntimeError("StageSurface context is not initialized.")
        with stage._device.context_guard():
            rect = stage.map()
            try:
                yield rect
            finally:
                stage.unmap()


@contextmanager
def readout_mode(mode):
    if mode == "baseline":
        yield
        return
    if mode != "hold_guard":
        raise ValueError(f"Unknown readout mode: {mode}")
    sys.path.insert(0, str(ROOT))
    from dxcam.core.stagesurf import StageSurface

    original = StageSurface.mapped
    StageSurface.mapped = guarded_mapped
    try:
        yield
    finally:
        StageSurface.mapped = original


def worker(args):
    before = acquisition.source_fingerprint()
    runtime_before = runtime_fingerprint()
    captured = io.StringIO()
    args.workload = "animated"
    # The shared worker releases its camera before this scope restores the patch.
    # Redirect only its final JSON; no per-frame instrumentation is introduced.
    with readout_mode(args.mode), redirect_stdout(captured):
        acquisition.worker(args)
    result = json.loads(captured.getvalue().strip().splitlines()[-1])
    after = acquisition.source_fingerprint()
    runtime_after = runtime_fingerprint()
    result.update(
        readout_mode=args.mode,
        worker_source_sha256_start=before,
        worker_source_sha256_end=after,
        worker_source_unchanged=before == after,
        worker_runtime_start=runtime_before,
        worker_runtime_end=runtime_after,
        worker_runtime_unchanged=runtime_before == runtime_after,
    )
    if before != after or runtime_before != runtime_after:
        result["error"] = "Runtime files changed during the worker trial."
    print(json.dumps(result), flush=True)


def configurations(args):
    configs = []
    for fps in dict.fromkeys(args.target_fps_values):
        effective_seen = set()
        for timeout in args.timeouts_ms:
            effective = min(timeout, 1000 // fps) if fps else timeout
            if effective in effective_seen:
                continue
            effective_seen.add(effective)
            configs.extend((mode, fps, timeout) for mode in dict.fromkeys(args.modes))
    return configs


def compare(args):
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    before = acquisition.source_fingerprint()
    runtime_before = runtime_fingerprint()
    configs = configurations(args)
    payload = {
        "experiment": "readout_scheduling",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "platform": platform.platform(),
        "cpu_logical_count": os.cpu_count(),
        "arguments": vars(args),
        "expected_trials": len(configs) * args.repeats,
        "trials": [],
        "renderers": [],
        "checkout_head": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "python_source_sha256_start": before,
        "runtime_start": runtime_before,
        "helper_sha256": {
            name: hashlib.sha256((ROOT / "benchmarks" / name).read_bytes()).hexdigest()
            for name in (
                "compare_readout_scheduling.py",
                "compare_acquisition_wait.py",
                "compare_capture.py",
                "controlled_animation.py",
            )
        },
        "limitations": [
            "Candidate holds the native device guard during CPU conversion; this may delay acquisition and other cameras.",
            "Lock order remains processor -> stage mapping lock -> device guard; existing Map/Unmap guards are retained.",
            "Single immediate consumer only; no slow-reader or multi-camera conclusion follows from this grid.",
            "The renderer requests 120 updates/s by default; actual visual delivery is compositor/display limited.",
            "CPU includes capture, conversion and marker decoding; 100% means one logical core.",
            "Memory is process working set/private commit, not GPU or total-system memory.",
            "Age runs from the DXGI source timestamp to completed read, before marker decoding.",
            "Shared worker excludes a read completing at/after the deadline, but includes its time in elapsed CPU/FPS denominators.",
            "No acquisition hot-path counters or timing wrappers are installed.",
            "Runtime provenance includes .py, .pyx and compiled .pyd files, excluding __pycache__; hashes are taken outside frame measurements.",
            "Group by readout_mode as well as target FPS and effective wait; do not merge the two modes.",
        ],
    }
    rng = random.Random(args.seed)

    def save():
        after = acquisition.source_fingerprint()
        runtime_after = runtime_fingerprint()
        payload["python_source_sha256_end"] = after
        payload["runtime_end"] = runtime_after
        payload["source_unchanged"] = (
            payload.get("source_unchanged", True)
            and before == after
            and all(
                trial.get("worker_source_sha256_start", before) == before
                and trial.get("worker_source_unchanged", True)
                for trial in payload["trials"]
            )
        )
        runtime_matches = runtime_before == runtime_after
        for trial in payload["trials"]:
            if "error" in trial and "worker_runtime_start" not in trial:
                continue
            valid = (
                trial.get("worker_source_sha256_start") == before
                and trial.get("worker_source_sha256_end") == before
                and trial.get("worker_source_unchanged") is True
                and trial.get("worker_runtime_start") == runtime_before
                and trial.get("worker_runtime_end") == runtime_before
                and trial.get("worker_runtime_unchanged") is True
            )
            trial["provenance_valid"] = valid
            if not valid:
                runtime_matches = False
                trial.setdefault(
                    "error", "Worker runtime provenance does not match the controller."
                )
        payload["runtime_unchanged"] = (
            payload.get("runtime_unchanged", True) and runtime_matches
        )
        if runtime_before != runtime_after and payload["trials"]:
            payload["trials"][-1]["provenance_valid"] = False
            payload["trials"][-1].setdefault(
                "error", "Runtime files changed during the experiment."
            )
        payload["completed_trials"] = sum("error" not in t for t in payload["trials"])
        output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    try:
        for repeat in range(1, args.repeats + 1):
            renderer, ready = acquisition.start_workload(args, "animated")
            try:
                ordered = list(configs)
                rng.shuffle(ordered)
                for mode, fps, timeout in ordered:
                    trial = {
                        "repeat": repeat,
                        "order": len(payload["trials"]) + 1,
                        "readout_mode": mode,
                        "target_fps": fps,
                        "requested_timeout_ms": timeout,
                    }
                    command = [
                        sys.executable,
                        "-I",
                        str(Path(__file__).resolve()),
                        "--worker",
                        "--mode",
                        mode,
                        "--target-fps",
                        str(fps),
                        "--timeout-ms",
                        str(timeout),
                        "--region",
                        *map(str, ready["region"]),
                        "--duration",
                        str(args.duration),
                        "--warmup",
                        str(args.warmup),
                    ]
                    try:
                        completed = subprocess.run(
                            command,
                            cwd=ROOT,
                            capture_output=True,
                            text=True,
                            timeout=args.duration + args.warmup + 20,
                        )
                        if completed.returncode:
                            raise RuntimeError(
                                completed.stderr[-6000:] or completed.stdout[-6000:]
                            )
                        trial.update(
                            json.loads(completed.stdout.strip().splitlines()[-1])
                        )
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
                                key: trial.get(key)
                                for key in (
                                    "order",
                                    "repeat",
                                    "readout_mode",
                                    "target_fps",
                                    "effective_timeout_ms",
                                    "process_cpu_percent_one_core",
                                    "source_updates_per_s",
                                    "visual_frames_per_s",
                                    "frame_age_ms",
                                    "error",
                                )
                            }
                        ),
                        flush=True,
                    )
                    if (
                        not payload["source_unchanged"]
                        or not payload["runtime_unchanged"]
                    ):
                        raise RuntimeError(
                            "Runtime source or compiled extension changed during the experiment."
                        )
            finally:
                try:
                    summary, stderr = renderer.communicate(input="stop\n", timeout=5)
                except (subprocess.TimeoutExpired, BrokenPipeError):
                    renderer.kill()
                    summary, stderr = renderer.communicate(timeout=5)
                payload["renderers"].append(
                    {
                        "repeat": repeat,
                        "ready": ready,
                        "summary": summary.strip(),
                        "stderr": stderr,
                        "returncode": renderer.returncode,
                    }
                )
    finally:
        save()
    print(f"Saved scheduling experiment: {output}", flush=True)
    return (
        0
        if payload["completed_trials"] == payload["expected_trials"]
        and payload["source_unchanged"]
        and payload["runtime_unchanged"]
        else 1
    )


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", default=str(ROOT / "benchmarks/results/readout_scheduling.json")
    )
    parser.add_argument("--duration", type=float, default=5)
    parser.add_argument("--warmup", type=float, default=1)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument("--source-fps", type=float, default=120)
    parser.add_argument("--seed", type=int, default=20260918)
    parser.add_argument("--target-fps-values", nargs="+", type=int, default=[0, 120])
    parser.add_argument("--timeouts-ms", nargs="+", type=int, default=[0, 10])
    parser.add_argument("--modes", nargs="+", choices=MODES, default=list(MODES))
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--mode", choices=MODES, help=argparse.SUPPRESS)
    parser.add_argument("--target-fps", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--timeout-ms", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--region", nargs=4, type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if (
        any(
            not math.isfinite(value) or value <= 0
            for value in (args.duration, args.warmup, args.source_fps)
        )
        or args.repeats < 1
    ):
        parser.error(
            "Duration, warmup, source FPS and repeats must be positive and finite."
        )
    if args.width < 400 or args.height < 160:
        parser.error("The workload must be at least 400x160 to contain its marker.")
    if any(value < 0 for value in args.target_fps_values) or any(
        not 0 <= value <= 1000 for value in args.timeouts_ms
    ):
        parser.error(
            "Target FPS must be nonnegative; acquisition timeouts must be in 0..1000."
        )
    if args.worker and (
        args.mode is None
        or args.target_fps is None
        or args.target_fps < 0
        or args.timeout_ms is None
        or not 0 <= args.timeout_ms <= 1000
        or args.region is None
    ):
        parser.error("Worker mode requires a mode, target FPS, timeout and region.")
    return args


if __name__ == "__main__":
    arguments = parse_args()
    if arguments.worker:
        worker(arguments)
    else:
        sys.exit(compare(arguments))
