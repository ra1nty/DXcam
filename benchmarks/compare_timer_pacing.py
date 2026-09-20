"""Compare two timer.py implementations without importing DXcam or capturing.

Defaults: three paired repeats, randomized variant order, the same randomized
60/120/240 FPS order within each pair, and two-second timing windows. Each
variant/repeat uses a fresh process and each case creates a fresh timer. Two
one-FPS cancellation probes run outside the pacing/CPU windows.

Freeze both timer files first, then run with the same interpreter:
    .venv/Scripts/python.exe -I benchmarks/compare_timer_pacing.py \
        --baseline-root .test/timer_baseline

Explicit --baseline-timer-path and --candidate-timer-path override the default
<source-root>/dxcam/util/timer.py, or <source-root>/timer.py for a standalone
snapshot. Results retain per-wait samples, coverage, errors, and file hashes.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import platform
import random
import subprocess
import sys
from threading import Event, Thread
import time


ROOT = Path(__file__).resolve().parents[1]
VARIANTS = ("baseline", "candidate")
PROBES = ("during_wait", "before_wait")


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def timer_path(root, override):
    if override:
        path = Path(override).resolve()
    else:
        path = root / "dxcam" / "util" / "timer.py"
        if not path.is_file():
            path = root / "timer.py"
    if not path.is_file():
        raise ValueError(f"Timer source does not exist: {path}")
    return path


def source_metadata(root, path):
    provenance = root / "provenance.json"
    snapshot = (
        json.loads(provenance.read_text(encoding="utf-8"))
        if provenance.is_file()
        else None
    )
    head = snapshot.get("git_head") if isinstance(snapshot, dict) else None
    if (root / ".git").exists():
        head = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True
        ).strip()
    return {
        "source_root": str(root),
        "timer_path": str(path),
        "timer_sha256": sha256(path),
        "git_head": head,
        "snapshot_metadata": snapshot,
    }


def load_timer(path):
    # Load only this file. Importing dxcam would initialize unrelated graphics
    # dependencies and could accidentally pick the installed package instead.
    sys.dont_write_bytecode = True
    name = "_dxcam_timer_pacing_subject"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot import timer source: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    source = path.read_bytes()
    # Read-only bytecode caches can still be loaded when dont_write_bytecode is
    # true. Compile the exact source bytes so recorded hashes identify the code.
    exec(compile(source, str(path), "exec"), module.__dict__)
    if Path(module.__file__).resolve() != path:
        raise RuntimeError("Imported timer does not match the requested file.")
    return module, hashlib.sha256(source).hexdigest()


def distribution(values):
    if not values:
        return None
    ordered = sorted(values)

    def percentile(fraction):
        position = (len(ordered) - 1) * fraction
        lo = math.floor(position)
        hi = math.ceil(position)
        return ordered[lo] + (ordered[hi] - ordered[lo]) * (position - lo)

    return {
        "count": len(values),
        "min": ordered[0],
        "mean": sum(values) / len(values),
        "p50": percentile(0.50),
        "p95": percentile(0.95),
        "p99": percentile(0.99),
        "max": ordered[-1],
    }


def next_tick(timer):
    value = getattr(timer, "_next_tick", None)
    if not isinstance(value, (int, float)) or not math.isfinite(value):
        raise RuntimeError("Timer must expose a finite perf_counter _next_tick.")
    return float(value)


def cleanup_timer(module, timer):
    module.cancel_timer(timer)
    close = getattr(module, "close_timer", None)
    if close is not None:
        close(timer)


def pacing_trial(module, fps, args):
    timer = module.create_high_resolution_timer()
    period = 1.0 / fps
    samples = []
    try:
        module.set_periodic_timer(timer, fps)
        warmup_deadline = time.perf_counter() + args.warmup
        warmup_ticks = 0
        while time.perf_counter() < warmup_deadline:
            if module.wait_for_timer(timer) is False:
                raise RuntimeError("Timer cancelled unexpectedly during warmup.")
            warmup_ticks += 1
            if warmup_ticks > max(1000, int(fps * (args.warmup + 1) * 8)):
                raise RuntimeError("Excessive timer returns during warmup.")

        # Rearm after warmup so the first interval has a defined schedule origin.
        # Timer creation, warmup and cleanup are outside the CPU timing window.
        module.set_periodic_timer(timer, fps)
        origin = next_tick(timer) - period
        started = time.perf_counter()
        cpu_started = time.process_time()
        measurement_deadline = started + args.duration
        previous = origin
        while time.perf_counter() < measurement_deadline:
            scheduled = next_tick(timer)
            wait_started = time.perf_counter()
            result = module.wait_for_timer(timer)
            completed = time.perf_counter()
            if result is False:
                raise RuntimeError("Timer cancelled unexpectedly during pacing.")
            interval_ms = (completed - previous) * 1000
            schedule_error_ms = (completed - scheduled) * 1000
            samples.append(
                {
                    "scheduled_offset_s": scheduled - origin,
                    "wait_started_offset_s": wait_started - origin,
                    "completed_offset_s": completed - origin,
                    "next_deadline_offset_s": next_tick(timer) - origin,
                    "interval_ms": interval_ms,
                    "interval_error_ms": interval_ms - period * 1000,
                    "schedule_error_ms": schedule_error_ms,
                    "lateness_ms": max(0.0, schedule_error_ms),
                }
            )
            previous = completed
            if len(samples) > max(1000, int(fps * (args.duration + 1) * 8)):
                raise RuntimeError("Excessive timer returns during measurement.")
        cpu_seconds = time.process_time() - cpu_started
        finished = time.perf_counter()
        elapsed = finished - started
    finally:
        cleanup_timer(module, timer)

    if not samples:
        raise RuntimeError("Timing window produced no samples.")
    return {
        "target_fps": fps,
        "duration_s": args.duration,
        "warmup_s": args.warmup,
        "warmup_ticks": warmup_ticks,
        "period_s": period,
        "schedule_origin_perf_s": origin,
        "measurement_start_offset_s": started - origin,
        "measurement_end_offset_s": finished - origin,
        "elapsed_s": elapsed,
        "deadline_overrun_ms": max(0.0, finished - measurement_deadline) * 1000,
        "cpu_seconds": cpu_seconds,
        "process_cpu_percent_one_core": cpu_seconds / elapsed * 100,
        "tick_count": len(samples),
        "effective_fps": len(samples) / elapsed,
        "interval_ms": distribution([s["interval_ms"] for s in samples]),
        "interval_error_ms": distribution([s["interval_error_ms"] for s in samples]),
        "absolute_interval_error_ms": distribution(
            [abs(s["interval_error_ms"]) for s in samples]
        ),
        "lateness_ms": distribution([s["lateness_ms"] for s in samples]),
        "schedule_error_ms": distribution([s["schedule_error_ms"] for s in samples]),
        "early_return_count": sum(s["schedule_error_ms"] < 0 for s in samples),
        "nonpositive_interval_count": sum(s["interval_ms"] <= 0 for s in samples),
        "deadline_resync_count": sum(
            s["next_deadline_offset_s"] - s["scheduled_offset_s"] > period * 1.5
            for s in samples
        ),
        "samples": samples,
    }


def cancellation_probe(module, mode, args):
    timer = module.create_high_resolution_timer()
    entered = Event()
    finished = Event()
    observations = {}
    waiter = None
    result = {"mode": mode, "target_fps": 1}

    def wait_once():
        observations["wait_start_perf_s"] = time.perf_counter()
        entered.set()
        try:
            value = module.wait_for_timer(timer)
            observations["wait_return_value"] = value
        except Exception as exc:
            observations["wait_error"] = repr(exc)
        finally:
            observations["wait_end_perf_s"] = time.perf_counter()
            finished.set()

    try:
        module.set_periodic_timer(timer, 1)
        result["scheduled_deadline_perf_s"] = next_tick(timer)
        waiter = Thread(target=wait_once, daemon=True)
        if mode == "during_wait":
            waiter.start()
            if not entered.wait(args.probe_timeout):
                raise RuntimeError("Waiter did not reach the timer call.")
            # This is a call-entry handshake, not instrumentation of the native
            # wait. Keep the delay explicit and reject an already-finished wait.
            if finished.wait(args.cancel_delay):
                raise RuntimeError("One-FPS wait finished before cancellation.")
        cancel_started = time.perf_counter()
        module.cancel_timer(timer)
        cancel_finished = time.perf_counter()
        if mode == "before_wait":
            waiter.start()
        if not finished.wait(args.probe_timeout):
            raise RuntimeError("Timer wait exceeded the cancellation probe deadline.")
        waiter.join(timeout=args.probe_timeout)
        if waiter.is_alive():
            raise RuntimeError("Timer waiter did not exit after returning.")
        if "wait_error" in observations:
            raise RuntimeError(observations["wait_error"])
        if mode == "during_wait" and observations["wait_end_perf_s"] < cancel_started:
            raise RuntimeError("Wait completed before cancel_timer was called.")
        result.update(
            cancel_start_perf_s=cancel_started,
            cancel_end_perf_s=cancel_finished,
            cancel_call_ms=(cancel_finished - cancel_started) * 1000,
            cancel_to_wait_return_ms=(observations["wait_end_perf_s"] - cancel_started)
            * 1000,
            wait_call_ms=(
                observations["wait_end_perf_s"] - observations["wait_start_perf_s"]
            )
            * 1000,
            configured_cancel_delay_s=args.cancel_delay if mode == "during_wait" else 0,
            completed=True,
        )
    except Exception as exc:
        result.update(error=repr(exc), completed=False)
    finally:
        result.update(observations)
        # Never close a handle under an active native wait. A failed probe's
        # daemon thread is contained by this disposable process and its timeout.
        if waiter is None or not waiter.is_alive():
            cleanup_timer(module, timer)
        else:
            result["cleanup_deferred_to_process_exit"] = True
    return result


def worker(args):
    path = Path(args.timer_path).resolve()
    before = sha256(path)
    result = {
        "variant": args.variant,
        "repeat": args.repeat,
        "source_root": str(Path(args.source_root).resolve()),
        "timer_path": str(path),
        "timer_sha256_start": before,
        "helper_sha256": sha256(Path(__file__).resolve()),
        "python": platform.python_version(),
        "executable": sys.executable,
        "platform": platform.platform(),
        "pid": os.getpid(),
        "fps_order": args.fps_values,
        "pacing_trials": [],
        "cancellation_probes": [],
    }
    try:
        module, loaded_hash = load_timer(path)
        result["timer_sha256_loaded"] = loaded_hash
        if loaded_hash != before:
            raise RuntimeError("Timer source changed before its module was loaded.")
        result["has_explicit_close_timer"] = hasattr(module, "close_timer")
        for fps in args.fps_values:
            result["pacing_trials"].append(pacing_trial(module, fps, args))
        for mode in PROBES:
            probe = cancellation_probe(module, mode, args)
            result["cancellation_probes"].append(probe)
            if "error" in probe:
                raise RuntimeError(f"Cancellation probe {mode}: {probe['error']}")
    except Exception as exc:
        result["error"] = repr(exc)
    finally:
        result["timer_sha256_end"] = sha256(path)
        result["source_unchanged"] = before == result["timer_sha256_end"]
        if not result["source_unchanged"]:
            result["error"] = "Timer source changed during this worker run."
    print(json.dumps(result), flush=True)


def compare(args):
    output = Path(args.output).resolve()
    roots = {
        variant: Path(getattr(args, f"{variant}_root")).resolve()
        for variant in VARIANTS
    }
    paths = {
        variant: timer_path(roots[variant], getattr(args, f"{variant}_timer_path"))
        for variant in VARIANTS
    }
    helper_hash = sha256(Path(__file__).resolve())
    payload = {
        "experiment": "timer_pacing",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "arguments": vars(args),
        "sources": {
            variant: source_metadata(roots[variant], paths[variant])
            for variant in VARIANTS
        },
        "helper_sha256_start": helper_hash,
        "cpu_logical_count": os.cpu_count(),
        "clock": {
            name: vars(time.get_clock_info(name))
            for name in ("perf_counter", "process_time")
        },
        "expected_runs": len(VARIANTS) * args.repeats,
        "expected_pacing_trials": len(VARIANTS) * args.repeats * len(args.fps_values),
        "expected_cancellation_probes": len(VARIANTS) * args.repeats * len(PROBES),
        "runs": [],
        "limitations": [
            "Timer-only measurements; no capture, processing, DWM, GPU, or power measurement.",
            "CPU includes this process's timer calls and sample bookkeeping; 100% is one logical core.",
            "Lateness is max(0, completion minus the timer's pre-wait _next_tick); signed errors are also retained.",
            "Intervals include the first scheduled period after rearm; effective FPS uses tick count divided by actual measured wall time.",
            "The final wait may cross the two-second deadline; that return and its full wall/CPU time are included.",
            "Raw deadlines reveal resynchronization; per-wait lateness alone does not describe long-term cadence drift.",
            "During-wait cancellation uses call-entry plus a recorded delay, not a hook inside the native wait.",
            "Cancellation latency ends when wait_for_timer returns; it is not whole-camera stop or recovery latency.",
            "Two-second tails have few samples; compare repeated trials and retain their variation.",
            "Only timer.py is imported directly; other files in the labeled source tree are provenance, not measured code.",
        ],
    }
    expected_pacing = Counter(
        (variant, repeat, fps)
        for repeat in range(1, args.repeats + 1)
        for variant in VARIANTS
        for fps in args.fps_values
    )
    expected_probes = Counter(
        (variant, repeat, mode)
        for repeat in range(1, args.repeats + 1)
        for variant in VARIANTS
        for mode in PROBES
    )
    output.parent.mkdir(parents=True, exist_ok=True)

    def save():
        pacing = Counter()
        probes = Counter()
        errors = []
        valid = sha256(Path(__file__).resolve()) == helper_hash
        for variant in VARIANTS:
            source = payload["sources"][variant]
            source["timer_sha256_end"] = sha256(paths[variant])
            valid &= source["timer_sha256"] == source["timer_sha256_end"]
        for run in payload["runs"]:
            variant, repeat = run["variant"], run["repeat"]
            expected_hash = payload["sources"][variant]["timer_sha256"]
            run["provenance_valid"] = (
                run.get("timer_sha256_start") == expected_hash
                and run.get("timer_sha256_loaded") == expected_hash
                and run.get("timer_sha256_end") == expected_hash
                and run.get("source_unchanged") is True
                and run.get("helper_sha256") == helper_hash
                and run.get("timer_path") == str(paths[variant])
            )
            valid &= run["provenance_valid"]
            if "error" in run:
                errors.append(
                    {"variant": variant, "repeat": repeat, "error": run["error"]}
                )
            for trial in run.get("pacing_trials", []):
                pacing[(variant, repeat, trial["target_fps"])] += 1
            for probe in run.get("cancellation_probes", []):
                if probe.get("completed") is True and "error" not in probe:
                    probes[(variant, repeat, probe["mode"])] += 1
        payload.update(
            helper_sha256_end=sha256(Path(__file__).resolve()),
            fingerprints_valid=bool(valid),
            completed_runs=sum("error" not in run for run in payload["runs"]),
            completed_pacing_trials=sum(pacing.values()),
            completed_cancellation_probes=sum(probes.values()),
            pacing_coverage_complete=pacing == expected_pacing,
            cancellation_coverage_complete=probes == expected_probes,
            errors=errors,
            complete=(
                len(payload["runs"]) == payload["expected_runs"]
                and pacing == expected_pacing
                and probes == expected_probes
                and not errors
                and bool(valid)
            ),
        )
        output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    rng = random.Random(args.seed)
    timeout = len(args.fps_values) * (args.duration + args.warmup + 1)
    timeout += 2 * args.probe_timeout + 10
    try:
        for repeat in range(1, args.repeats + 1):
            fps_order = list(args.fps_values)
            variants = list(VARIANTS)
            rng.shuffle(fps_order)
            rng.shuffle(variants)
            for variant in variants:
                command = [
                    sys.executable,
                    "-I",
                    str(Path(__file__).resolve()),
                    "--worker",
                    "--source-root",
                    str(roots[variant]),
                    "--timer-path",
                    str(paths[variant]),
                    "--variant",
                    variant,
                    "--repeat",
                    str(repeat),
                    "--duration",
                    str(args.duration),
                    "--warmup",
                    str(args.warmup),
                    "--cancel-delay",
                    str(args.cancel_delay),
                    "--probe-timeout",
                    str(args.probe_timeout),
                    "--fps-values",
                    *map(str, fps_order),
                ]
                try:
                    completed = subprocess.run(
                        command,
                        cwd=ROOT,
                        capture_output=True,
                        text=True,
                        timeout=timeout,
                    )
                    if completed.returncode:
                        raise RuntimeError(
                            completed.stderr[-6000:] or completed.stdout[-6000:]
                        )
                    run = json.loads(completed.stdout.strip().splitlines()[-1])
                    if run.get("variant") != variant or run.get("repeat") != repeat:
                        raise RuntimeError(
                            "Worker returned the wrong variant or repeat."
                        )
                    run["worker_stderr"] = completed.stderr
                except (
                    OSError,
                    subprocess.SubprocessError,
                    ValueError,
                    IndexError,
                    RuntimeError,
                ) as exc:
                    run = {"variant": variant, "repeat": repeat, "error": repr(exc)}
                payload["runs"].append(run)
                save()
                print(
                    json.dumps(
                        {
                            "variant": variant,
                            "repeat": repeat,
                            "pacing_trials": len(run.get("pacing_trials", [])),
                            "cancellation_probes": len(
                                run.get("cancellation_probes", [])
                            ),
                            "error": run.get("error"),
                        }
                    ),
                    flush=True,
                )
                if not payload["fingerprints_valid"]:
                    return 1
    finally:
        save()
    print(f"Saved timer pacing comparison: {output}", flush=True)
    return 0 if payload["complete"] else 1


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-root", default=str(ROOT / ".test/timer_baseline"))
    parser.add_argument("--candidate-root", default=str(ROOT))
    parser.add_argument("--baseline-timer-path")
    parser.add_argument("--candidate-timer-path")
    parser.add_argument(
        "--output", default=str(ROOT / "benchmarks/results/timer_pacing.json")
    )
    parser.add_argument("--duration", type=float, default=2.0)
    parser.add_argument("--warmup", type=float, default=0.2)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--fps-values", nargs="+", type=int, default=[60, 120, 240])
    parser.add_argument("--cancel-delay", type=float, default=0.05)
    parser.add_argument("--probe-timeout", type=float, default=2.0)
    parser.add_argument("--seed", type=int, default=20260920)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--source-root", help=argparse.SUPPRESS)
    parser.add_argument("--timer-path", help=argparse.SUPPRESS)
    parser.add_argument("--variant", choices=VARIANTS, help=argparse.SUPPRESS)
    parser.add_argument("--repeat", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not math.isfinite(args.duration) or not 0 < args.duration <= 30:
        parser.error("Duration must be finite and in (0, 30] seconds.")
    if not math.isfinite(args.warmup) or not 0 <= args.warmup <= 10:
        parser.error("Warmup must be finite and in [0, 10] seconds.")
    if not math.isfinite(args.cancel_delay) or not 0 < args.cancel_delay < 1:
        parser.error("Cancellation delay must be finite and between 0 and 1 second.")
    if not math.isfinite(args.probe_timeout) or not 1.25 <= args.probe_timeout <= 10:
        parser.error("Probe timeout must be finite and in [1.25, 10] seconds.")
    if args.repeats < 1 or any(not 0 < fps <= 1000 for fps in args.fps_values):
        parser.error("Repeats must be positive; FPS values must be in 1..1000.")
    if len(set(args.fps_values)) != len(args.fps_values):
        parser.error("FPS values must not repeat.")
    if args.worker and (
        args.source_root is None
        or args.timer_path is None
        or args.variant is None
        or args.repeat is None
        or args.repeat < 1
    ):
        parser.error(
            "Worker mode requires source root, timer path, variant and repeat."
        )
    return args


if __name__ == "__main__":
    arguments = parse_args()
    if arguments.worker:
        worker(arguments)
    else:
        sys.exit(compare(arguments))
