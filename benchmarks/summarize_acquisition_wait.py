"""Print audited Markdown summaries of acquisition-wait benchmark JSON files.

Usage: python benchmarks/summarize_acquisition_wait.py metrics.json [more.json ...]
Only the Python standard library is required. Input files are never modified.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
import math
from pathlib import Path
import statistics
import sys


COUNTERS = (
    "invalid_markers",
    "duplicate_timestamps",
    "negative_frame_ages",
)
METRICS = (
    "process_cpu_percent_one_core",
    "source_updates_per_s",
    "visual_frames_per_s",
    "stop_seconds",
)
AGE_PERCENTILES = ("p50", "p95", "p99")


def number(value) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def nonnegative(value) -> bool:
    return number(value) and value >= 0


def count(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def cell(value) -> str:
    return str(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def table(headers, rows) -> None:
    print("| " + " | ".join(map(cell, headers)) + " |")
    print("| " + " | ".join("---" for _ in headers) + " |")
    for row in rows:
        print("| " + " | ".join(map(cell, row)) + " |")


def median(values, scale=1) -> str:
    measured = [value * scale for value in values if value is not None]
    return f"{statistics.median(measured):.2f}" if measured else "N/A"


def expected_trial_keys(payload) -> Counter | None:
    """Reconstruct the requested matrix, including each repeat."""
    args = payload.get("arguments")
    if not isinstance(args, dict):
        return None
    repeats = args.get("repeats")
    workloads = args.get("workloads")
    fps_values = args.get("target_fps_values")
    timeouts = args.get("timeouts_ms")
    if (
        not count(repeats)
        or repeats == 0
        or not isinstance(workloads, list)
        or not workloads
        or any(value not in ("static", "animated") for value in workloads)
        or not isinstance(fps_values, list)
        or not fps_values
        or not isinstance(timeouts, list)
        or not timeouts
        or not all(count(value) for value in fps_values + timeouts)
        or any(value > 1000 for value in timeouts)
    ):
        return None
    return Counter(
        (repeat, workload, fps, effective)
        for repeat in range(1, repeats + 1)
        for workload in workloads
        for fps in fps_values
        for effective in {min(wait, 1000 // fps) if fps else wait for wait in timeouts}
    )


def coverage_problems(trials, expected) -> list[str]:
    if expected is None:
        return ["Expected trial coverage cannot be verified from the run arguments."]
    actual = Counter()
    malformed = 0
    for trial in trials:
        if (
            not isinstance(trial, dict)
            or trial.get("workload") not in ("static", "animated")
            or any(
                not count(trial.get(key))
                for key in ("repeat", "target_fps", "effective_timeout_ms")
            )
        ):
            malformed += 1
            continue
        actual[
            (
                trial["repeat"],
                trial["workload"],
                trial["target_fps"],
                trial["effective_timeout_ms"],
            )
        ] += 1
    problems = []
    if malformed:
        problems.append(
            f"{malformed} trial(s) lack valid repeat/configuration metadata."
        )
    for label, difference in (
        ("Missing", expected - actual),
        ("Unexpected or duplicate", actual - expected),
    ):
        if difference:
            details = "; ".join(
                f"repeat={repeat}, {workload}, FPS={fps}, wait={wait} ms (x{amount})"
                for (repeat, workload, fps, wait), amount in sorted(difference.items())
            )
            problems.append(f"{label} {sum(difference.values())} trial(s): {details}.")
    return problems


def trial_problems(trial) -> tuple[str | None, list[str]]:
    if not isinstance(trial, dict):
        return "incomplete", ["trial is not an object"]
    if trial.get("error"):
        return "failed", [str(trial["error"])]

    missing = []
    if trial.get("workload") not in ("static", "animated"):
        missing.append("workload")
    for key in ("target_fps", "effective_timeout_ms", "source_updates"):
        if not count(trial.get(key)):
            missing.append(key)
    for key in METRICS:
        if not nonnegative(trial.get(key)):
            missing.append(key)
    if not number(trial.get("elapsed_s")) or trial["elapsed_s"] <= 0:
        missing.append("elapsed_s")
    for key in COUNTERS:
        if not count(trial.get(key)):
            missing.append(key)
    memory = trial.get("memory_after")
    for key in ("working_set_mib", "private_mib"):
        if not isinstance(memory, dict) or not nonnegative(memory.get(key)):
            missing.append("memory_after." + key)
    ages = trial.get("frame_age_ms")
    for key in AGE_PERCENTILES:
        if not isinstance(ages, dict) or key not in ages:
            missing.append("frame_age_ms." + key)
        elif ages[key] is None:
            if count(trial.get("source_updates")) and trial["source_updates"] > 0:
                missing.append("frame_age_ms." + key)
        elif not number(ages[key]):
            missing.append("frame_age_ms." + key)
    if missing:
        return "incomplete", ["missing or invalid: " + ", ".join(missing)]

    violations = [f"{key}={trial[key]}" for key in COUNTERS if trial[key]]
    if any(ages[key] is not None and ages[key] < 0 for key in AGE_PERCENTILES):
        violations.append("negative frame-age statistic")
    if trial["workload"] == "static" and trial["visual_frames_per_s"] > 0:
        violations.append("static workload reports new visual frames after warmup")
    if violations:
        return "invalid", violations
    return None, []


def summarize(path: Path) -> bool:
    print(f"### {cell(path)}\n")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        print(f"Audit **FAILED**: cannot read complete JSON: {cell(exc)}.\n")
        return False
    if not isinstance(payload, dict) or not isinstance(payload.get("trials"), list):
        print("Audit **FAILED**: expected an object containing a trials list.\n")
        return False

    trials = payload["trials"]
    expected_keys = expected_trial_keys(payload)
    expected = None if expected_keys is None else sum(expected_keys.values())
    coverage_issues = coverage_problems(trials, expected_keys)
    source_ok = payload.get("source_unchanged") is True
    before = payload.get("python_source_sha256_start")
    after = payload.get("python_source_sha256_end")
    if before is not None and after is not None and before != after:
        source_ok = False
    groups = defaultdict(list)
    excluded = Counter()
    counters = Counter()
    issues = []
    candidates = 0
    for index, trial in enumerate(trials, 1):
        if isinstance(trial, dict):
            for key in COUNTERS:
                if count(trial.get(key)):
                    counters[key] += trial[key]
        category, problems = trial_problems(trial)
        if category is not None:
            excluded[category] += 1
            label = trial.get("order", index) if isinstance(trial, dict) else index
            issues.append((label, category, "; ".join(problems)))
            continue
        candidates += 1
        if source_ok:
            key = (
                trial["workload"],
                trial["target_fps"],
                trial["effective_timeout_ms"],
            )
            groups[key].append(trial)
    included = candidates if source_ok else 0
    progress = str(len(trials))
    if expected is not None:
        progress += f"/{expected} expected"
        if len(trials) < expected:
            progress += " (partial run)"
    print(
        f"Audit: {progress} trials; {included} summarized; "
        f"{excluded['failed']} failed, {excluded['incomplete']} incomplete, "
        f"{excluded['invalid']} invalid; "
        f"invalid markers={counters['invalid_markers']}, "
        f"nonincreasing timestamps={counters['duplicate_timestamps']}, "
        f"negative ages={counters['negative_frame_ages']}; "
        f"source unchanged={'yes' if source_ok else 'FAILED or unverified'}.\n"
    )
    if coverage_issues:
        print("**Coverage audit FAILED:**\n")
        for problem in coverage_issues:
            print(f"- {cell(problem)}")
        print()
    if not source_ok:
        print(
            f"**No measurements summarized:** {candidates} otherwise usable trials "
            "were excluded because source consistency is not verified.\n"
        )
    if issues:
        print("Excluded trials (all recorded errors are shown):\n")
        table(("Trial", "Status", "Reason"), issues)
        print()
    if groups:
        rows = []
        for (workload, fps, timeout), group in sorted(groups.items()):
            cpus = [trial["process_cpu_percent_one_core"] for trial in group]
            ages = [
                "N/A"
                if workload == "static"
                else median(trial["frame_age_ms"][key] for trial in group)
                for key in AGE_PERCENTILES
            ]
            rows.append(
                (
                    workload,
                    fps,
                    timeout,
                    len(group),
                    f"{median(cpus)} [{min(cpus):.2f}, {max(cpus):.2f}]",
                    median(trial["source_updates_per_s"] for trial in group),
                    median(trial["visual_frames_per_s"] for trial in group),
                    *ages,
                    median(trial["memory_after"]["working_set_mib"] for trial in group),
                    median(trial["memory_after"]["private_mib"] for trial in group),
                    median((trial["stop_seconds"] for trial in group), scale=1000),
                )
            )
        table(
            (
                "Workload",
                "Target FPS",
                "Effective wait ms",
                "Trials",
                "CPU % one core, median [min, max]",
                "Source FPS",
                "Visual FPS",
                "Age p50 ms",
                "Age p95 ms",
                "Age p99 ms",
                "End RSS MiB",
                "End private MiB",
                "Stop ms",
            ),
            rows,
        )
        print()
    elif source_ok:
        print("No usable completed trials to summarize.\n")
    return source_ok and not excluded and not coverage_issues


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "paths", nargs="+", type=Path, help="Benchmark metrics JSON files"
    )
    args = parser.parse_args()
    print(
        "All table values are medians across trials unless stated otherwise. "
        "Age columns are medians of each trial's percentile, not pooled percentiles. "
        "Static frame ages are not interpreted.\n"
    )
    passed = True
    for path in args.paths:
        passed = summarize(path) and passed
    print(
        "Method limits: CPU covers the capture process, including conversion and "
        "marker decoding; 100% means one logical core. RSS is process working set "
        "and private memory is process private commit; neither measures total GPU "
        "or DWM memory. Frame age runs from source presentation to completed read. "
        "Only the test region is static; desktop updates elsewhere may still be counted.\n"
    )
    print(
        "A read completing at or after the trial deadline is omitted from frame "
        "counts and age samples, while its time remains in elapsed time and CPU. "
        "This can exclude one boundary frame per trial (about 0.2 FPS for a "
        "five-second trial). Missing percentile samples appear as N/A."
    )
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
