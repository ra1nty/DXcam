"""Instrumented DXGI phase diagnostics; timings are not primary benchmark CPU data.

Only Map/Unmap, read processing, copy, and device-guard entry are wrapped. The hot
AcquireNextFrame polling loop is untouched. Run after other capture benchmarks.

Use the current checkout's environment, for example:
    .venv/Scripts/python.exe benchmarks/profile_acquisition_wait.py --source-fps 120

Each case runs in a fresh subprocess. The JSON contains numeric phase samples,
not captured pixels. The default grid includes 60/120 Hz GDI submissions and
0/60/120 target FPS with requested acquisition waits of 0/10 ms.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from contextlib import contextmanager
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
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
MARKER = (1, 0, 1, 0, 0, 1, 0, 1)


def source_provenance():
    """Fingerprint the source actually imported by workers, outside measurement."""
    digest = hashlib.sha256()
    paths = sorted((ROOT / "dxcam").rglob("*.py"))
    paths.extend(
        [
            ROOT / "pyproject.toml",
            Path(__file__).resolve(),
            ROOT / "benchmarks/controlled_animation.py",
        ]
    )
    for path in paths:
        digest.update(path.relative_to(ROOT).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    result = {"source_sha256": digest.hexdigest()}
    for name, arguments in (
        ("git_commit", ["rev-parse", "HEAD"]),
        ("git_status", ["status", "--short"]),
    ):
        try:
            completed = subprocess.run(
                ["git", *arguments],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=5,
                check=True,
            )
            result[name] = completed.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            result[name] = None
    return result


def dependency_versions():
    result = {}
    for name in ("dxcam", "numpy", "opencv-python", "comtypes"):
        try:
            result[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            result[name] = None
    return result


def distribution(values):
    if not values:
        return {"count": 0, "mean": None, "p50": None, "p95": None, "max": None}
    ordered = sorted(values)

    def percentile(fraction):
        position = (len(ordered) - 1) * fraction
        lower = int(position)
        upper = min(lower + 1, len(ordered) - 1)
        return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)

    return {
        "count": len(values),
        "mean": statistics.fmean(values),
        "p50": percentile(0.5),
        "p95": percentile(0.95),
        "max": ordered[-1],
    }


def source_id(frame):
    samples = frame[24, 24:344:8, :3].mean(axis=1)
    if len(samples) != 40 or any(32 < value < 223 for value in samples):
        return None
    bits = tuple(int(value > 128) for value in samples)
    if bits[:8] != MARKER or any(a == b for a, b in zip(bits[8:24], bits[24:40])):
        return None
    return sum(bit << index for index, bit in enumerate(bits[8:24]))


def worker(args):
    sys.path.insert(0, str(ROOT))
    import cv2
    import dxcam
    from dxcam.core.device import Device
    from dxcam.core.stagesurf import StageSurface
    from dxcam.dxcam import DXCamera

    cv2.setNumThreads(1)
    phases = defaultdict(list)
    copies = {}
    local = threading.local()
    measurement = {"start": float("inf"), "end": float("inf")}

    def measured(start, end):
        return measurement["start"] <= start <= end < measurement["end"]

    def record(name, start, end):
        if measured(start, end):
            phases[name].append((end - start) * 1000)

    original_map = StageSurface.map
    original_unmap = StageSurface.unmap
    original_process = DXCamera._process_stage
    original_copy = DXCamera._copy_region_to_surface
    original_guard = Device.context_guard

    def profiled_map(self):
        previous = getattr(local, "phase", "other")
        local.phase = "map"
        started = time.perf_counter()
        try:
            return original_map(self)
        finally:
            ended = time.perf_counter()
            local.last_map = (started, ended)
            local.phase = previous
            record("stage_map_including_guard_ms", started, ended)

    def profiled_unmap(self):
        previous = getattr(local, "phase", "other")
        local.phase = "unmap"
        started = time.perf_counter()
        try:
            return original_unmap(self)
        finally:
            ended = time.perf_counter()
            local.last_unmap = (started, ended)
            local.phase = previous
            record("stage_unmap_including_guard_ms", started, ended)

    def profiled_process(self, *positional, **kwargs):
        previous = getattr(local, "phase", "other")
        local.phase = "process_stage"
        started = time.perf_counter()
        try:
            return original_process(self, *positional, **kwargs)
        finally:
            ended = time.perf_counter()
            local.last_process = (started, ended)
            local.phase = previous
            record("process_stage_including_map_ms", started, ended)

    def profiled_copy(self, *positional, **kwargs):
        started = time.perf_counter()
        try:
            return original_copy(self, *positional, **kwargs)
        finally:
            ended = time.perf_counter()
            record("copy_region_inside_guard_ms", started, ended)
            if measured(started, ended):
                timestamp = self._duplicator.ticks_to_seconds(
                    self._duplicator.latest_frame_ticks
                )
                copies[timestamp] = (started, ended)
                phases["source_age_at_copy_start_ms"].append(
                    (started - timestamp) * 1000
                )

    @contextmanager
    def profiled_guard(self):
        phase = getattr(local, "phase", "other")
        thread_name = threading.current_thread().name
        # In this runtime the producer's only explicit device guard encloses
        # _copy_region_to_surface; it is entered before the copy wrapper runs.
        if phase == "other" and thread_name == "DXCamera":
            phase = "copy"
        started = time.perf_counter()
        entered = None
        try:
            with original_guard(self):
                entered = time.perf_counter()
                yield
        finally:
            # Record after Leave, so sample collection never extends the guard.
            if entered is not None:
                record(
                    f"guard_entry_ms/{thread_name}/{phase}",
                    started,
                    entered,
                )
                if phase in ("map", "unmap"):
                    setattr(local, f"last_{phase}_guard_ms", (entered - started) * 1000)

    StageSurface.map = profiled_map
    StageSurface.unmap = profiled_unmap
    DXCamera._process_stage = profiled_process
    DXCamera._copy_region_to_surface = profiled_copy
    Device.context_guard = profiled_guard

    camera = None
    last_timestamp = None
    records = []
    read_timeouts = 0
    invalid_markers = 0
    backwards_timestamps = 0
    duplicate_timestamps = 0
    try:
        camera = dxcam.create(
            region=tuple(args.region),
            backend="dxgi",
            processor_backend="cv2",
            output_color="BGR",
        )
        camera.start(
            target_fps=args.target_fps, video_mode=False, frame_timeout_ms=args.wait_ms
        )
        warmup_deadline = time.perf_counter() + args.warmup
        while time.perf_counter() < warmup_deadline:
            remaining = warmup_deadline - time.perf_counter()
            if remaining <= 0:
                break
            result = camera.get_latest_frame(
                with_timestamp=True,
                after_timestamp=last_timestamp,
                timeout=min(0.5, remaining),
            )
            if result is not None:
                last_timestamp = result[1]

        started = time.perf_counter()
        deadline = started + args.duration
        measurement.update(start=started, end=deadline)
        while time.perf_counter() < deadline:
            read_started = time.perf_counter()
            remaining = deadline - read_started
            if remaining <= 0:
                break
            result = camera.get_latest_frame(
                with_timestamp=True,
                after_timestamp=last_timestamp,
                timeout=min(0.5, remaining),
            )
            completed = time.perf_counter()
            if completed >= deadline:
                break
            if result is None:
                read_timeouts += 1
                continue
            frame, timestamp = result
            if last_timestamp is not None:
                duplicate_timestamps += int(timestamp == last_timestamp)
                backwards_timestamps += int(timestamp < last_timestamp)
            last_timestamp = timestamp
            identifier = source_id(frame)
            invalid_markers += int(identifier is None)
            process_started, process_ended = local.last_process
            map_started, map_ended = local.last_map
            unmap_started, unmap_ended = local.last_unmap
            copy = copies.get(timestamp)
            sample = {
                "timestamp": timestamp,
                "source_id": identifier,
                "frame_age_ms": (completed - timestamp) * 1000,
                "source_age_at_process_start_ms": (process_started - timestamp) * 1000,
                "read_wait_before_processing_ms": (process_started - read_started)
                * 1000,
                "process_stage_ms": (process_ended - process_started) * 1000,
                "map_ms": (map_ended - map_started) * 1000,
                "unmap_ms": (unmap_ended - unmap_started) * 1000,
                "conversion_between_map_and_unmap_ms": (unmap_started - map_ended)
                * 1000,
                "map_guard_entry_ms": local.last_map_guard_ms,
                "unmap_guard_entry_ms": local.last_unmap_guard_ms,
                "read_completion_after_process_ms": (completed - process_ended) * 1000,
            }
            if copy is not None:
                sample.update(
                    source_age_at_copy_start_ms=(copy[0] - timestamp) * 1000,
                    copy_to_process_start_ms=(process_started - copy[1]) * 1000,
                )
            records.append(sample)
        elapsed = time.perf_counter() - started
    finally:
        try:
            if camera is not None:
                camera.release()
        finally:
            StageSurface.map = original_map
            StageSurface.unmap = original_unmap
            DXCamera._process_stage = original_process
            DXCamera._copy_region_to_surface = original_copy
            Device.context_guard = original_guard

    effective_wait = args.wait_ms
    if args.target_fps > 0:
        effective_wait = min(effective_wait, int(1000 / args.target_fps))
    print(
        json.dumps(
            {
                "environment": {
                    "python": sys.version,
                    "executable": sys.executable,
                    "platform": platform.platform(),
                    "dxcam_file": str(Path(dxcam.__file__).resolve()),
                    "distribution_versions": dependency_versions(),
                    "cv2_threads": cv2.getNumThreads(),
                },
                "target_fps": args.target_fps,
                "requested_wait_ms": args.wait_ms,
                "effective_wait_ms": effective_wait,
                "source_fps": args.worker_source_fps,
                "region": args.region,
                "warmup_s": args.warmup,
                "duration_s": args.duration,
                "elapsed_s": elapsed,
                "frame_count": len(records),
                "delivered_fps": len(records) / args.duration,
                "read_timeouts": read_timeouts,
                "invalid_markers": invalid_markers,
                "backwards_timestamps": backwards_timestamps,
                "duplicate_timestamps": duplicate_timestamps,
                "phase_distributions": {
                    name: distribution(values)
                    for name, values in sorted(phases.items())
                },
                "frame_distributions": {
                    name: distribution([row[name] for row in records if name in row])
                    for name in (
                        "frame_age_ms",
                        "source_age_at_process_start_ms",
                        "read_wait_before_processing_ms",
                        "process_stage_ms",
                        "map_ms",
                        "unmap_ms",
                        "conversion_between_map_and_unmap_ms",
                        "map_guard_entry_ms",
                        "unmap_guard_entry_ms",
                        "read_completion_after_process_ms",
                        "source_age_at_copy_start_ms",
                        "copy_to_process_start_ms",
                    )
                },
                "frames": records,
            }
        ),
        flush=True,
    )


def run(args):
    if args.duration <= 0 or args.warmup < 0:
        raise ValueError("Duration must be positive and warmup nonnegative.")
    executable = Path(args.python).resolve()
    environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    results = []
    renderers = []
    provenance_before = source_provenance()
    try:
        for source_fps in args.source_fps:
            case_count = len(args.target_fps_values) * len(args.waits_ms)
            maximum = case_count * (args.duration + args.warmup + 15) + 15
            command = [
                str(executable),
                "-I",
                str(ROOT / "benchmarks/controlled_animation.py"),
                "--width",
                "1280",
                "--height",
                "720",
                "--fps",
                str(source_fps),
                "--max-seconds",
                str(maximum),
            ]
            renderer = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=environment,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            summary = {"requested_source_fps": source_fps}
            renderers.append(summary)
            try:
                with ThreadPoolExecutor(max_workers=1) as pool:
                    future = pool.submit(renderer.stdout.readline)
                    try:
                        ready_line = future.result(timeout=10)
                    except FutureTimeoutError:
                        renderer.kill()
                        raise RuntimeError(
                            "Renderer did not initialize within ten seconds."
                        )
                if not ready_line:
                    raise RuntimeError(f"Renderer failed: {renderer.stderr.read()}")
                ready = json.loads(ready_line)
                summary["ready"] = ready
                for target_fps in args.target_fps_values:
                    for wait_ms in args.waits_ms:
                        worker_command = [
                            str(executable),
                            "-I",
                            str(Path(__file__).resolve()),
                            "--worker",
                            "--target-fps",
                            str(target_fps),
                            "--wait-ms",
                            str(wait_ms),
                            "--worker-source-fps",
                            str(source_fps),
                            "--warmup",
                            str(args.warmup),
                            "--duration",
                            str(args.duration),
                            "--region",
                            *map(str, ready["region"]),
                        ]
                        print(
                            f"phase diagnostic: source={source_fps}, target={target_fps}, wait={wait_ms}",
                            flush=True,
                        )
                        try:
                            completed = subprocess.run(
                                worker_command,
                                capture_output=True,
                                text=True,
                                cwd=ROOT,
                                env=environment,
                                timeout=args.duration + args.warmup + 15,
                            )
                            if completed.returncode:
                                raise RuntimeError(
                                    completed.stderr[-4000:] or completed.stdout[-4000:]
                                )
                            result = json.loads(
                                completed.stdout.strip().splitlines()[-1]
                            )
                            print(
                                json.dumps(
                                    {
                                        "target_fps": target_fps,
                                        "wait_ms": wait_ms,
                                        "source_fps": source_fps,
                                        "delivered_fps": result["delivered_fps"],
                                        "frame_distributions": result[
                                            "frame_distributions"
                                        ],
                                        "phase_distributions": result[
                                            "phase_distributions"
                                        ],
                                    }
                                ),
                                flush=True,
                            )
                        except (
                            subprocess.TimeoutExpired,
                            RuntimeError,
                            ValueError,
                        ) as exc:
                            result = {
                                "target_fps": target_fps,
                                "requested_wait_ms": wait_ms,
                                "source_fps": source_fps,
                                "error": str(exc),
                            }
                            print(json.dumps(result), flush=True)
                        results.append(result)
            finally:
                try:
                    remaining, stderr = renderer.communicate(input="stop\n", timeout=5)
                    if remaining.strip():
                        summary["final"] = json.loads(
                            remaining.strip().splitlines()[-1]
                        )
                    if stderr.strip():
                        summary["stderr"] = stderr[-4000:]
                except (subprocess.TimeoutExpired, BrokenPipeError):
                    renderer.kill()
                    renderer.communicate()
                    summary["forced_stop"] = True
    finally:
        provenance_after = source_provenance()
        output = Path(args.output).resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(
                {
                    "created_utc": datetime.now(timezone.utc).isoformat(),
                    "provenance_before": provenance_before,
                    "provenance_after": provenance_after,
                    "source_unchanged": (
                        provenance_before["source_sha256"]
                        == provenance_after["source_sha256"]
                    ),
                    "arguments": vars(args),
                    "renderers": renderers,
                    "trials": results,
                    "limitations": [
                        "Monkeypatch overhead affects timings: diagnostic data, not primary benchmark CPU data.",
                        "AcquireNextFrame is deliberately not wrapped; zero-timeout polling is untouched.",
                        "Phase timings are nested: do not add their percentiles.",
                        "Guard entry measures Python/native entry overhead plus contention, not GPU execution.",
                        "Map and Unmap each include their device-guard entry wait; full processing includes both and conversion.",
                        "The producer's only explicit device guard is labeled copy without wrapping its hot acquisition loop.",
                        "Source-to-copy-start includes native acquisition and producer guard entry waiting.",
                        "Only phases fully contained in the measurement interval contribute.",
                        "A first measured read may lack paired copy data if its copy occurred during warmup.",
                        "A separate GDI renderer does not guarantee presentation at its requested source FPS.",
                        "Source age uses the DXGI presentation timestamp, not the renderer's drawing timestamp.",
                        "A single fixed-order diagnostic trial per case does not establish general latency rankings.",
                        "Distribution versions describe the environment; dxcam_file identifies the imported checkout.",
                    ],
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"Saved phase diagnostics to {output}", flush=True)


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--python",
        default=sys.executable,
        help="Python environment used for every renderer and worker.",
    )
    parser.add_argument("--duration", type=float, default=3)
    parser.add_argument("--warmup", type=float, default=1)
    parser.add_argument(
        "--source-fps", type=int, nargs="+", choices=[60, 120], default=[60, 120]
    )
    parser.add_argument(
        "--target-fps-values",
        type=int,
        nargs="+",
        choices=[0, 60, 120],
        default=[0, 60, 120],
    )
    parser.add_argument(
        "--waits-ms", type=int, nargs="+", choices=[0, 10], default=[0, 10]
    )
    parser.add_argument(
        "--output",
        default=str(ROOT / "benchmarks/results/acquisition_wait_phases.json"),
    )
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--region", type=int, nargs=4, help=argparse.SUPPRESS)
    parser.add_argument("--target-fps", type=int, default=0, help=argparse.SUPPRESS)
    parser.add_argument("--wait-ms", type=int, default=0, help=argparse.SUPPRESS)
    parser.add_argument(
        "--worker-source-fps", type=int, default=120, help=argparse.SUPPRESS
    )
    return parser.parse_args()


if __name__ == "__main__":
    arguments = parse_args()
    if arguments.worker:
        worker(arguments)
    else:
        run(arguments)
