"""Measure DXGI acquisition waits without instrumenting the acquisition hot path.

Each trial uses a fresh process and the same current checkout. Only metrics are
saved. The static workload freezes the test window, not the entire desktop.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import random
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "benchmarks"))
from compare_capture import decode_source_id, distribution, source_fingerprint  # noqa: E402


def process_memory() -> dict[str, float | int]:
    class ProcessMemoryCounters(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
            ("PrivateUsage", ctypes.c_size_t),
        ]

    kernel32, psapi = ctypes.windll.kernel32, ctypes.windll.psapi
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    psapi.GetProcessMemoryInfo.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
    counters = ProcessMemoryCounters()
    counters.cb = ctypes.sizeof(counters)
    if not psapi.GetProcessMemoryInfo(
        kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb
    ):
        raise ctypes.WinError()
    return {
        "working_set_mib": counters.WorkingSetSize / 2**20,
        "private_mib": counters.PrivateUsage / 2**20,
        "page_faults": counters.PageFaultCount,
    }


def worker(args):
    sys.path.insert(0, str(ROOT))
    import cv2
    import dxcam

    cv2.setNumThreads(1)
    camera = dxcam.create(
        region=tuple(args.region),
        backend="dxgi",
        output_color="BGR",
        processor_backend="cv2",
    )
    try:
        camera.start(target_fps=args.target_fps, frame_timeout_ms=args.timeout_ms)
        last_timestamp = None
        warmup_ids = set()
        deadline = time.perf_counter() + args.warmup
        while (remaining := deadline - time.perf_counter()) > 0:
            result = camera.get_latest_frame(
                with_timestamp=True,
                after_timestamp=last_timestamp,
                timeout=min(0.1, remaining),
            )
            if result is not None:
                frame, last_timestamp = result
                source_id = decode_source_id(frame)
                if source_id is not None:
                    warmup_ids.add(source_id)
        if not warmup_ids:
            raise RuntimeError("No valid controlled-window marker during warmup.")

        memory_before = process_memory()
        ages, waits = [], []
        visual_ids = set()
        source_updates = invalid_markers = duplicate_timestamps = negative_ages = (
            none_reads
        ) = 0
        started = time.perf_counter()
        cpu_started = time.process_time()
        deadline = started + args.duration
        while (remaining := deadline - time.perf_counter()) > 0:
            read_started = time.perf_counter()
            result = camera.get_latest_frame(
                with_timestamp=True,
                after_timestamp=last_timestamp,
                timeout=min(0.1, remaining),
            )
            completed = time.perf_counter()
            if completed >= deadline:
                break
            if result is None:
                none_reads += 1
                continue
            frame, timestamp = result
            waits.append((completed - read_started) * 1000)
            duplicate_timestamps += int(
                last_timestamp is not None and timestamp <= last_timestamp
            )
            source_updates += 1
            age = (completed - timestamp) * 1000
            negative_ages += int(age < 0)
            ages.append(age)
            source_id = decode_source_id(frame)
            invalid_markers += int(source_id is None)
            if source_id is not None:
                visual_ids.add(source_id)
            last_timestamp = timestamp
        elapsed = time.perf_counter() - started
        cpu_seconds = time.process_time() - cpu_started
        memory_after = process_memory()
        stopped = time.perf_counter()
        camera.stop()
        stop_seconds = time.perf_counter() - stopped
        effective_timeout_ms = (
            min(args.timeout_ms, int(1000 / args.target_fps))
            if args.target_fps > 0
            else args.timeout_ms
        )
        result = {
            "workload": args.workload,
            "target_fps": args.target_fps,
            "requested_timeout_ms": args.timeout_ms,
            "effective_timeout_ms": effective_timeout_ms,
            "duration_s": args.duration,
            "elapsed_s": elapsed,
            "warmup_s": args.warmup,
            "source_updates": source_updates,
            "source_updates_per_s": source_updates / elapsed,
            "visual_ids": sorted(visual_ids),
            "warmup_visual_ids": sorted(warmup_ids),
            "unique_visual_frames": len(visual_ids - warmup_ids),
            "visual_frames_per_s": len(visual_ids - warmup_ids) / elapsed,
            "duplicate_timestamps": duplicate_timestamps,
            "invalid_markers": invalid_markers,
            "negative_frame_ages": negative_ages,
            "timed_out_reads": none_reads,
            "frame_age_ms": distribution(ages),
            "read_wait_ms": distribution(waits),
            "cpu_seconds": cpu_seconds,
            "process_cpu_percent_one_core": cpu_seconds / elapsed * 100,
            "memory_before": memory_before,
            "memory_after": memory_after,
            "stop_seconds": stop_seconds,
            "cv2_threads": cv2.getNumThreads(),
            "region": args.region,
            "device_info": dxcam.device_info(),
            "output_info": dxcam.output_info(),
            "python": platform.python_version(),
            "dxcam_file": str(Path(dxcam.__file__).resolve()),
            "dependencies": {
                name: importlib.metadata.version(name)
                for name in ("numpy", "opencv-python", "comtypes")
            },
        }
    finally:
        camera.release()
    print(json.dumps(result), flush=True)


def start_workload(args, workload):
    command = [
        sys.executable,
        "-I",
        str(ROOT / "benchmarks/controlled_animation.py"),
        "--width",
        str(args.width),
        "--height",
        str(args.height),
        "--fps",
        str(args.source_fps),
        "--max-seconds",
        "3600",
    ]
    if workload == "static":
        command.append("--static")
    renderer = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        ready = pool.submit(renderer.stdout.readline)
        try:
            line = ready.result(timeout=10)
        except TimeoutError:
            renderer.kill()
            raise RuntimeError("Renderer initialization timed out.")
    if not line:
        raise RuntimeError(renderer.stderr.read())
    return renderer, json.loads(line)


def compare(args):
    if args.duration <= 0 or args.warmup <= 0 or args.repeats < 1:
        raise ValueError("Duration, warmup and repeats must be positive.")
    if any(not 0 <= value <= 1000 for value in args.timeouts_ms) or any(
        fps < 0 for fps in args.target_fps_values
    ):
        raise ValueError("Timeouts must be in 0..1000; target FPS must be nonnegative.")
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    before = source_fingerprint()
    payload = {
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "platform": platform.platform(),
        "cpu_logical_count": os.cpu_count(),
        "arguments": vars(args),
        "trials": [],
        "renderers": [],
        "checkout_head": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "python_source_sha256_start": before,
        "benchmark_script_sha256": hashlib.sha256(
            Path(__file__).read_bytes()
        ).hexdigest(),
        "renderer_script_sha256": hashlib.sha256(
            (ROOT / "benchmarks/controlled_animation.py").read_bytes()
        ).hexdigest(),
        "limitations": [
            "Static freezes only the capture region; other desktop activity can trigger source presents.",
            "CPU and memory are capture-process metrics; they exclude the renderer, DWM and GPU allocations.",
            "100% CPU means one logical core, not the whole computer.",
            "Frame age is source presentation to completed read; static ages are not interpreted as performance.",
            "GDI source submissions are compositor/display limited; source IDs validate actual content.",
            "No acquisition counters or timers are inserted into the hot path.",
        ],
    }
    rng = random.Random(args.seed)

    def save():
        payload["python_source_sha256_end"] = source_fingerprint()
        payload["source_unchanged"] = before == payload["python_source_sha256_end"]
        output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    try:
        for repeat in range(args.repeats):
            workloads = list(args.workloads)
            rng.shuffle(workloads)
            for workload in workloads:
                renderer, info = start_workload(args, workload)
                try:
                    configs = []
                    for fps in args.target_fps_values:
                        effective_seen = set()
                        for timeout in args.timeouts_ms:
                            effective = (
                                min(timeout, int(1000 / fps)) if fps else timeout
                            )
                            if effective in effective_seen:
                                continue
                            effective_seen.add(effective)
                            configs.append((fps, timeout))
                    rng.shuffle(configs)
                    for fps, timeout in configs:
                        command = [
                            sys.executable,
                            "-I",
                            str(Path(__file__).resolve()),
                            "--worker",
                            "--workload",
                            workload,
                            "--target-fps",
                            str(fps),
                            "--timeout-ms",
                            str(timeout),
                            "--region",
                            *map(str, info["region"]),
                            "--duration",
                            str(args.duration),
                            "--warmup",
                            str(args.warmup),
                        ]
                        trial = {
                            "repeat": repeat + 1,
                            "workload": workload,
                            "target_fps": fps,
                            "requested_timeout_ms": timeout,
                            "order": len(payload["trials"]) + 1,
                        }
                        completed = subprocess.run(
                            command,
                            cwd=ROOT,
                            capture_output=True,
                            text=True,
                            timeout=args.duration + args.warmup + 20,
                        )
                        if completed.returncode:
                            trial["error"] = (
                                completed.stderr[-6000:] or completed.stdout[-6000:]
                            )
                        else:
                            trial.update(
                                json.loads(completed.stdout.strip().splitlines()[-1])
                            )
                        payload["trials"].append(trial)
                        save()
                        print(
                            json.dumps(
                                {
                                    key: trial.get(key)
                                    for key in (
                                        "order",
                                        "repeat",
                                        "workload",
                                        "target_fps",
                                        "requested_timeout_ms",
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
                finally:
                    remaining, stderr = renderer.communicate(input="stop\n", timeout=5)
                    payload["renderers"].append(
                        {
                            "repeat": repeat + 1,
                            "workload": workload,
                            "ready": info,
                            "summary": remaining.strip(),
                            "stderr": stderr,
                        }
                    )
    finally:
        save()
    print(f"Saved metrics only: {output}", flush=True)


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", default=str(ROOT / "benchmarks/results/acquisition_wait.json")
    )
    parser.add_argument("--duration", type=float, default=5)
    parser.add_argument("--warmup", type=float, default=1)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument("--source-fps", type=float, default=120)
    parser.add_argument("--seed", type=int, default=20260916)
    parser.add_argument(
        "--workloads",
        nargs="+",
        choices=("static", "animated"),
        default=["static", "animated"],
    )
    parser.add_argument("--target-fps-values", nargs="+", type=int, default=[0, 120])
    parser.add_argument(
        "--timeouts-ms", nargs="+", type=int, default=[0, 1, 4, 8, 10, 16, 33]
    )
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--workload", choices=("static", "animated"), help=argparse.SUPPRESS
    )
    parser.add_argument("--target-fps", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--timeout-ms", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--region", nargs=4, type=int, help=argparse.SUPPRESS)
    return parser.parse_args()


if __name__ == "__main__":
    arguments = parse_args()
    if arguments.worker:
        worker(arguments)
    else:
        compare(arguments)
