"""Compare isolated DXcam versions with bounded, timestamp-deduplicated trials.

See capture_comparison.md for setup, interpretation, and limitations.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import time
import tomllib

ROOT = Path(__file__).resolve().parents[1]
MARKER = (1, 0, 1, 0, 0, 1, 0, 1)


def source_fingerprint() -> str:
    digest = hashlib.sha256()
    for path in sorted((ROOT / "dxcam").rglob("*.py")):
        digest.update(path.relative_to(ROOT).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def distribution(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {key: None for key in ("mean", "p50", "p95", "p99", "max")}
    ordered = sorted(values)

    def percentile(fraction: float) -> float:
        position = (len(ordered) - 1) * fraction
        lower = int(position)
        upper = min(lower + 1, len(ordered) - 1)
        return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)

    return {"mean": statistics.fmean(values), "p50": percentile(0.5),
            "p95": percentile(0.95), "p99": percentile(0.99), "max": ordered[-1]}


def decode_source_id(frame) -> int | None:
    """Validate marker/complement before treating the workload as visible."""
    samples = frame[24, 24:344:8, :3].mean(axis=1)
    if len(samples) != 40 or any(32 < value < 223 for value in samples):
        return None
    bits = tuple(int(value > 128) for value in samples)
    if bits[:8] != MARKER or any(a == b for a, b in zip(bits[8:24], bits[24:40])):
        return None
    return sum(bit << index for index, bit in enumerate(bits[8:24]))


def worker(args: argparse.Namespace) -> None:
    if args.variant == "dev":
        sys.path.insert(0, str(ROOT))
    import cv2
    import dxcam

    cv2.setNumThreads(1)
    if args.variant == "pypi030":
        if importlib.metadata.version("dxcam") != "0.3.0" or Path(dxcam.__file__).resolve().parent == ROOT / "dxcam":
            raise RuntimeError("Baseline import is not the isolated published dxcam==0.3.0.")
    region = tuple(args.region)
    camera = dxcam.create(region=region, backend="dxgi", processor_backend="cv2", output_color="BGR")
    try:
        camera.start(region=region, target_fps=args.target_fps, video_mode=False)
        warmup_deadline = time.perf_counter() + args.warmup
        last_ts = None
        while time.perf_counter() < warmup_deadline:
            if camera.latest_frame_time in (None, last_ts):
                time.sleep(args.poll_ms / 1000)
                continue
            result = camera.get_latest_frame(with_timestamp=True)
            if result is not None:
                last_ts = result[1]

        ages: list[float] = []
        calls: list[float] = []
        timestamps: set[float] = set()
        source_ids: set[int] = set()
        duplicate_reads = none_reads = invalid_markers = backwards_timestamps = 0
        negative_ages = 0
        unchanged_polls = 0
        started = time.perf_counter()
        cpu_started = time.process_time()
        deadline = started + args.duration
        while time.perf_counter() < deadline:
            if camera.latest_frame_time in (None, last_ts):
                unchanged_polls += 1
                time.sleep(args.poll_ms / 1000)
                continue
            call_started = time.perf_counter()
            result = camera.get_latest_frame(with_timestamp=True)
            completed = time.perf_counter()
            if completed >= deadline:
                break
            calls.append((completed - call_started) * 1000)
            if result is None:
                none_reads += 1
            else:
                frame, timestamp = result
                if last_ts is not None and timestamp < last_ts:
                    backwards_timestamps += 1
                if timestamp in timestamps or timestamp == last_ts:
                    duplicate_reads += 1
                else:
                    timestamps.add(timestamp)
                    age = (completed - timestamp) * 1000
                    negative_ages += int(age < 0)
                    ages.append(age)
                    source_id = decode_source_id(frame)
                    if source_id is None:
                        invalid_markers += 1
                    else:
                        source_ids.add(source_id)
                last_ts = timestamp
            if args.consumer_delay_ms:
                time.sleep(args.consumer_delay_ms / 1000)
        cpu_seconds = time.process_time() - cpu_started
        elapsed = time.perf_counter() - started
        result = {
            "variant": args.variant,
            "dxcam_version": (tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
                              if args.variant == "dev" else importlib.metadata.version("dxcam")),
            "distribution_metadata_version": importlib.metadata.version("dxcam"),
            "dxcam_file": str(Path(dxcam.__file__).resolve()), "python": platform.python_version(),
            "dependencies": {name: importlib.metadata.version(name)
                             for name in ("numpy", "opencv-python", "comtypes")},
            "cv2_threads": cv2.getNumThreads(), "region": region, "backend": "dxgi",
            "capture_output_size": [camera.width, camera.height],
            "capture_rotation_degrees": camera.rotation_angle,
            "device_info": dxcam.device_info(), "output_info": dxcam.output_info(),
            "processor_backend": "cv2", "output_color": "BGR", "target_fps": args.target_fps,
            "consumer_delay_ms": args.consumer_delay_ms, "warmup_s": args.warmup,
            "unchanged_frame_poll_ms": args.poll_ms, "unchanged_polls": unchanged_polls,
            "requested_duration_s": args.duration, "elapsed_s": elapsed,
            "deadline_overrun_s": max(0, elapsed - args.duration),
            "reads": len(calls), "unique_timestamps": len(timestamps),
            "unique_frames_per_s": len(timestamps) / args.duration,
            "unique_source_ids": len(source_ids), "source_frames_per_s": len(source_ids) / args.duration,
            "duplicate_reads": duplicate_reads, "none_reads": none_reads,
            "invalid_source_markers": invalid_markers, "backwards_timestamps": backwards_timestamps,
            "negative_frame_ages": negative_ages, "frame_age_ms": distribution(ages),
            "read_call_ms": distribution(calls), "cpu_seconds": cpu_seconds,
            "process_cpu_percent_one_core": cpu_seconds / elapsed * 100,
        }
    finally:
        camera.release()
    print(json.dumps(result), flush=True)


def compare(args: argparse.Namespace) -> None:
    if (args.duration <= 0 or args.warmup < 0 or args.repeats < 1 or args.poll_ms <= 0
            or args.target_fps < 0 or any(delay < 0 for delay in args.consumer_delays_ms)):
        raise ValueError("Duration/repeats/poll must be positive; warmup/delays/target FPS nonnegative.")
    baseline = Path(args.baseline_python).resolve()
    current = Path(args.dev_python).resolve()
    for executable in (baseline, current):
        if not executable.is_file():
            raise FileNotFoundError(executable)
    delays = args.consumer_delays_ms
    source_at_start = source_fingerprint()
    maximum = len(delays) * args.repeats * 2 * (args.duration + args.warmup + 15) + 15
    command = [str(current), "-I", str(ROOT / "benchmarks" / "controlled_animation.py"),
               "--width", str(args.width), "--height", str(args.height),
               "--fps", str(args.source_fps), "--max-seconds", str(maximum)]
    environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    workload = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, env=environment,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    results = []
    workload_summary = None
    try:
        # The renderer initializes synchronously; hard-stop it if initialization stalls.
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            ready_future = pool.submit(workload.stdout.readline)
            try:
                ready_line = ready_future.result(timeout=10)
            except TimeoutError:
                workload.kill()
                raise RuntimeError("Controlled renderer did not initialize within 10 seconds.")
        if not ready_line:
            raise RuntimeError(f"Controlled renderer failed: {workload.stderr.read()}")
        workload_info = json.loads(ready_line)
        region = workload_info["region"]
        for delay in delays:
            for repeat in range(args.repeats):
                variants = ("pypi030", "dev") if repeat % 2 == 0 else ("dev", "pypi030")
                for variant in variants:
                    executable = baseline if variant == "pypi030" else current
                    worker_command = [str(executable), "-I", str(Path(__file__).resolve()), "--worker",
                                      "--variant", variant, "--region", *map(str, region),
                                      "--duration", str(args.duration), "--warmup", str(args.warmup),
                                      "--target-fps", str(args.target_fps),
                                      "--poll-ms", str(args.poll_ms),
                                      "--consumer-delay-ms", str(delay)]
                    print(f"{variant}: repeat {repeat + 1}/{args.repeats}, consumer delay {delay} ms", flush=True)
                    try:
                        completed = subprocess.run(worker_command, capture_output=True, text=True,
                                                   cwd=ROOT / ".test", env=environment,
                                                   timeout=args.duration + args.warmup + 15)
                        if completed.returncode:
                            raise RuntimeError(completed.stderr[-5000:] or completed.stdout[-5000:])
                        trial = json.loads(completed.stdout.strip().splitlines()[-1])
                        trial["repeat"] = repeat + 1
                        results.append(trial)
                    except (subprocess.TimeoutExpired, RuntimeError, ValueError) as error:
                        results.append({"variant": variant, "repeat": repeat + 1,
                                        "consumer_delay_ms": delay, "error": str(error)})
                    print(json.dumps(results[-1]), flush=True)
    finally:
        try:
            remaining, stderr = workload.communicate(input="stop\n", timeout=5)
            if remaining.strip():
                workload_summary = json.loads(remaining.strip().splitlines()[-1])
            if stderr.strip():
                print(stderr, file=sys.stderr)
        except (subprocess.TimeoutExpired, BrokenPipeError):
            workload.kill()
            workload.communicate()
        git_head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True)
        git_status = subprocess.run(["git", "status", "--short"], cwd=ROOT, capture_output=True, text=True)
        source_at_end = source_fingerprint()
        metadata = {
            "created_utc": datetime.now(timezone.utc).isoformat(), "platform": platform.platform(),
            "checkout_head": git_head.stdout.strip(), "checkout_status": git_status.stdout.strip(),
            "python_source_sha256": source_at_end,
            "python_source_sha256_start": source_at_start,
            "source_unchanged_during_comparison": source_at_start == source_at_end,
            "cpu_logical_count": os.cpu_count(), "arguments": vars(args),
            "workload": workload_summary, "trials": results,
            "limitations": ["GDI submissions are compositor/display limited, not guaranteed presented FPS.",
                            "Frame age is DXGI last-present timestamp to completed read, not input latency.",
                            "Only timestamp-unique reads count as captured frames; source IDs verify visible content.",
                            "Process CPU excludes the separate animation process and initialization/warmup."],
        }
        output = Path(args.output).resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
        print(f"Saved metrics only to {output}", flush=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-python", default=str(ROOT / ".test/benchmark-030/Scripts/python.exe"))
    parser.add_argument("--dev-python", default=sys.executable)
    parser.add_argument("--output", default=str(ROOT / "benchmarks/results/capture_comparison.json"))
    parser.add_argument("--duration", type=float, default=8)
    parser.add_argument("--warmup", type=float, default=2)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument("--source-fps", type=float, default=120)
    parser.add_argument("--target-fps", type=int, default=120)
    parser.add_argument("--poll-ms", type=float, default=1)
    parser.add_argument("--consumer-delays-ms", nargs="+", type=float, default=[0, 30])
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--variant", choices=("pypi030", "dev"), help=argparse.SUPPRESS)
    parser.add_argument("--region", nargs=4, type=int, help=argparse.SUPPRESS)
    parser.add_argument("--consumer-delay-ms", type=float, default=0, help=argparse.SUPPRESS)
    return parser.parse_args()


if __name__ == "__main__":
    arguments = parse_args()
    if arguments.worker:
        worker(arguments)
    else:
        compare(arguments)
