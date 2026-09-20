"""Validate real DXGI/WGC pixels and recovery; display changes are opt-in.

The default checks current settings. --transitions temporarily changes the
selected output, with an independent restoration watchdog. Captured images
stay in memory; the JSON report contains metadata and sampled pixel values.
"""

from __future__ import annotations

import argparse
import ctypes
from datetime import datetime, timezone
import faulthandler
import hashlib
import json
import os
from pathlib import Path
import platform
import queue
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def expected_pixel(spec, x, y):
    """Resolve the renderer's literal rectangles, independently of DXcam."""
    color = None
    for patch in spec["rectangles"]:
        left, top, right, bottom = patch["rect"]
        if left <= x < right and top <= y < bottom:
            color = patch["bgr"]
    if color is None:
        raise ValueError(f"Pattern does not cover {x}, {y}")
    return color


def check_frame(frame, spec, region=None):
    import numpy as np

    if region is None:
        region = (0, 0, spec["width"], spec["height"])
    left, top, right, bottom = region
    expected_shape = (bottom - top, right - left, 3)
    if frame.shape != expected_shape or frame.dtype != np.uint8:
        return {
            "ok": False,
            "shape": list(frame.shape),
            "expected_shape": list(expected_shape),
            "dtype": str(frame.dtype),
        }
    samples = [
        p for p in spec["samples"] if left <= p["x"] < right and top <= p["y"] < bottom
    ]
    # Also check literal ROI boundaries and an interior grid, including crops
    # with no named sample. No expected values come from another processor.
    for row in range(5):
        y = top + row * (bottom - top - 1) // 4
        for col in range(5):
            x = left + col * (right - left - 1) // 4
            samples.append(
                dict(name=f"grid_{row}_{col}", x=x, y=y, bgr=expected_pixel(spec, x, y))
            )
    failures = []
    for sample in samples:
        actual = frame[sample["y"] - top, sample["x"] - left].tolist()
        if actual != sample["bgr"]:
            failures.append({**sample, "actual": actual})
    return {
        "ok": not failures,
        "shape": list(frame.shape),
        "checked_samples": len(samples),
        "failure_count": len(failures),
        "failures": failures[:12],
    }


def roi_cases(width, height):
    return [
        (0, 0, min(137, width), min(131, height)),
        (max(0, width - 119), max(0, height - 103), width, height),
        (width // 2 - 63, height // 2 - 47, width // 2 + 74, height // 2 + 56),
        (width - 1, height - 1, width, height),
    ]


def fingerprints():
    paths = sorted((ROOT / "dxcam").rglob("*.py"))
    paths += sorted((ROOT / "dxcam").rglob("*.pyd"))
    paths += sorted((ROOT / "benchmarks").glob("*geometry*.py"))
    return {
        p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in paths
    }


def set_dpi_awareness():
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.SetProcessDpiAwarenessContext.argtypes = [wintypes.HANDLE]
    user32.SetProcessDpiAwarenessContext.restype = wintypes.BOOL
    user32.SetThreadDpiAwarenessContext.argtypes = [wintypes.HANDLE]
    user32.SetThreadDpiAwarenessContext.restype = wintypes.HANDLE
    user32.GetThreadDpiAwarenessContext.argtypes = []
    user32.GetThreadDpiAwarenessContext.restype = wintypes.HANDLE
    user32.AreDpiAwarenessContextsEqual.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    user32.AreDpiAwarenessContextsEqual.restype = wintypes.BOOL
    requested = ctypes.c_void_p(-4)
    process_set = bool(user32.SetProcessDpiAwarenessContext(requested))
    previous = user32.SetThreadDpiAwarenessContext(requested)
    if not previous:
        raise ctypes.WinError(ctypes.get_last_error())
    effective = bool(
        user32.AreDpiAwarenessContextsEqual(
            user32.GetThreadDpiAwarenessContext(), requested
        )
    )
    if not effective:
        raise RuntimeError("Per-monitor-v2 DPI context was not established")
    return {
        "requested": "per_monitor_v2",
        "effective_per_monitor_v2": effective,
        "process_set_succeeded": process_set,
    }


def enumerate_outputs():
    import dxcam

    factory = dxcam._get_factory()
    return [
        {
            "device_index": d,
            "output_index": o,
            "device_name": output.devicename,
            "resolution": list(output.resolution),
            "rotation": output.rotation_angle,
            "attached": output.attached_to_desktop,
        }
        for d, outputs in enumerate(factory.outputs)
        for o, output in enumerate(outputs)
    ]


class Observations:
    """Observe successful native copies/recovery without altering return values."""

    def __init__(self, camera):
        from dxcam._libs.d3d11 import D3D11_TEXTURE2D_DESC

        self.camera = camera
        self.copies = []
        self.recoveries = []
        self.generation = 0
        self.lock = threading.Lock()
        original_copy = camera._copy_region_to_surface
        original_recover = camera._recover_output

        def observe_copy(region, stage):
            # Called within the production device guard. Only native GetDesc
            # and plain Python fields are inspected here; no extra frame lock.
            desc = D3D11_TEXTURE2D_DESC()
            camera._duplicator.texture.GetDesc(ctypes.byref(desc))
            result = original_copy(region, stage)
            observation = {
                "generation": self.generation,
                "region": list(region),
                "source_texture": [int(desc.Width), int(desc.Height)],
                "texture_format": int(desc.Format),
                "stage_size": [stage.width, stage.height],
                "public_size": [camera.width, camera.height],
                "public_rotation": camera.rotation_angle,
                "effective_rotation": camera._capture_rotation_angle,
                "capture_surface_size": list(camera._capture_surface_size),
                "output_name": camera._output.devicename,
                "output_rotation": camera._output.rotation_angle,
                "frame_pool_dimensions": getattr(
                    camera._duplicator, "_frame_pool_dimensions", None
                ),
            }
            with self.lock:
                if (not self.copies or observation != self.copies[-1]) and len(
                    self.copies
                ) < 100:
                    self.copies.append(observation)
            return result

        def observe_recover():
            started = time.perf_counter()
            original_recover()
            completed = not camera._recovery_pending
            with self.lock:
                if completed:
                    self.generation += 1
                self.recoveries.append(
                    {
                        "completed": completed,
                        "elapsed_s": time.perf_counter() - started,
                        "generation": self.generation,
                        "size": [camera.width, camera.height],
                        "rotation": camera.rotation_angle,
                    }
                )

        camera._copy_region_to_surface = observe_copy
        camera._recover_output = observe_recover

    def snapshot(self):
        with self.lock:
            return {
                "copies": list(self.copies),
                "recoveries": list(self.recoveries),
                "completed_recovery_generations": self.generation,
            }


def wait_for_pixels(camera, spec, timeout_s=8):
    last_timestamp = None
    deadline = time.monotonic() + timeout_s
    last_check = {"ok": False, "reason": "no_frame"}
    attempts = 0
    started = time.perf_counter()
    while time.monotonic() < deadline:
        result = camera.get_latest_frame(
            with_timestamp=True, after_timestamp=last_timestamp, timeout=0.3
        )
        if result is None:
            continue
        frame, last_timestamp = result
        attempts += 1
        last_check = check_frame(frame, spec)
        if last_check["ok"]:
            break
    return {
        **last_check,
        "attempts": attempts,
        "timestamp": last_timestamp,
        "elapsed_s": time.perf_counter() - started,
    }


def verify_rois(camera, spec):
    import numpy as np

    results = []
    for region in roi_cases(spec["width"], spec["height"]):
        for method in ("grab", "grab_into"):
            deadline = time.monotonic() + 3
            checked = {"ok": False, "reason": "no_frame"}
            while time.monotonic() < deadline:
                if method == "grab":
                    frame = camera.grab(region=region)
                else:
                    frame = np.full(
                        (region[3] - region[1], region[2] - region[0], 3),
                        173,
                        dtype=np.uint8,
                    )
                    if not camera.grab_into(frame, region=region):
                        frame = None
                if frame is not None:
                    checked = check_frame(frame, spec, region)
                    if checked["ok"]:
                        break
                time.sleep(0.01)
            results.append({"region": list(region), "method": method, **checked})
    return results


def verify_geometry(camera, spec, observations):
    from dxcam._libs.dxgi import DXGI_OUTPUT_DESC

    # A separate descriptor observes the native state without refreshing the
    # shared Output object and thereby influencing recovery.
    native = DXGI_OUTPUT_DESC()
    camera._output.output.GetDesc(ctypes.byref(native))
    rect = native.DesktopCoordinates
    native_size = [rect.right - rect.left, rect.bottom - rect.top]
    native_rotation = (0, 0, 90, 180, 270)[int(native.Rotation)]
    expected_source = list(native_size)
    expected_effective = native_rotation if camera.backend == "dxgi" else 0
    if expected_effective in (90, 270):
        expected_source.reverse()
    latest_copy = observations["copies"][-1] if observations["copies"] else {}
    checks = {
        "native_size": native_size == [spec["width"], spec["height"]],
        "public_size": [camera.width, camera.height] == native_size,
        "public_rotation": camera.rotation_angle == native_rotation,
        "effective_rotation": camera._capture_rotation_angle == expected_effective,
        "source_texture": latest_copy.get("source_texture") == expected_source,
        "staging_size": latest_copy.get("stage_size") == expected_source,
        "source_format": latest_copy.get("texture_format") == 87,
    }
    if camera.backend == "winrt":
        checks["pool_dimensions"] = (
            list(camera._duplicator._frame_pool_dimensions) == native_size
        )
    return {
        "ok": all(checks.values()),
        "checks": checks,
        "native_size": native_size,
        "native_rotation": native_rotation,
        "public_rotation": camera.rotation_angle,
        "public_size": [camera.width, camera.height],
        "expected_source_texture": expected_source,
        "last_copy": latest_copy,
    }


def capture_server(args):
    # The oracle describes the rendered window, excluding an unrelated pointer
    # overlay. This setting applies only to this fresh diagnostic process.
    os.environ["DXCAM_WINRT_CURSOR_CAPTURE"] = "0"
    os.environ["DXCAM_WINRT_BORDER_REQUIRED"] = "0"
    from benchmarks.geometry_pattern import pattern_spec
    import dxcam
    import numpy as np
    from dxcam.core.winrt_duplicator import WinRTDuplicator

    wgc_sizes = []
    original_mismatch = WinRTDuplicator._frame_size_mismatch

    def observe_mismatch(duplicator, frame):
        mismatch = original_mismatch(duplicator, frame)
        item = duplicator._capture_item.size
        item_data = {
            "content": [int(frame.content_size.width), int(frame.content_size.height)],
            "item": [int(item.width), int(item.height)],
            "pool": duplicator._frame_pool_dimensions,
            "output": list(duplicator._output.resolution),
            "rejected": bool(mismatch),
        }
        if (not wgc_sizes or item_data != wgc_sizes[-1]) and len(wgc_sizes) < 100:
            wgc_sizes.append(item_data)
        return mismatch

    WinRTDuplicator._frame_size_mismatch = observe_mismatch
    cameras = {}
    observers = {}
    retained = {}
    workers = {}

    def release_retained(backend):
        value = retained.pop(backend, None)
        if value is None:
            return None
        context, lease, pixels = value
        try:
            now = cameras[backend]._process_stage(
                stage=lease.stage,
                frame_width=lease.frame_width,
                frame_height=lease.frame_height,
                rotation_angle=lease.rotation_angle,
            )
            result = {
                "ok": bool(np.array_equal(pixels, now)),
                "retired_before_release": lease.slot.retired,
                "shape": list(pixels.shape),
            }
        finally:
            context.__exit__(None, None, None)
        result["released_after_last_reader"] = lease.slot._released
        result["ok"] &= (
            not result["retired_before_release"] or result["released_after_last_reader"]
        )
        return result

    try:
        for backend in args.backends:
            camera = dxcam.create(
                device_idx=args.device_index,
                output_idx=args.output_index,
                backend=backend,
                output_color="BGR",
                processor_backend=args.processor,
            )
            cameras[backend] = camera
            observers[backend] = Observations(camera)
            camera.start(target_fps=30, frame_timeout_ms=0)
            workers[backend] = camera._DXCamera__worker
        print(
            json.dumps(
                {
                    "event": "ready",
                    "outputs": enumerate_outputs(),
                    "winrt_session_options": {
                        "cursor_capture": cameras[
                            "winrt"
                        ]._duplicator._session.is_cursor_capture_enabled,
                        "border_required": cameras[
                            "winrt"
                        ]._duplicator._session.is_border_required,
                    }
                    if "winrt" in cameras
                    else None,
                }
            ),
            flush=True,
        )
        for line in sys.stdin:
            command = json.loads(line)
            if command.get("stop"):
                break
            spec = pattern_spec(command["width"], command["height"], command["epoch"])
            results = {}
            for backend, camera in cameras.items():
                verified = wait_for_pixels(camera, spec)
                worker = camera._DXCamera__worker
                same_worker = worker is workers[backend] and worker.is_running()
                old_lease = release_retained(backend)
                stopping = time.perf_counter()
                camera.stop()
                stop_s = time.perf_counter() - stopping
                observations = observers[backend].snapshot()
                geometry = verify_geometry(camera, spec, observations)
                rois = verify_rois(camera, spec) if verified["ok"] else []
                results[backend] = {
                    "ok": verified["ok"]
                    and geometry["ok"]
                    and same_worker
                    and bool(rois)
                    and all(r["ok"] for r in rois)
                    and (old_lease is None or old_lease["ok"]),
                    "threaded_full": verified,
                    "same_worker_through_transition": same_worker,
                    "retained_old_frame": old_lease,
                    "stop_s": stop_s,
                    "rois": rois,
                    "geometry": geometry,
                    "observations": observers[backend].snapshot(),
                }
                # Restart only AFTER checking the live transition and static
                # GPU ROI paths. This worker stays active through the next change.
                camera.start(target_fps=30, frame_timeout_ms=0)
                workers[backend] = camera._DXCamera__worker
                restarted = wait_for_pixels(camera, spec)
                results[backend]["restart"] = restarted
                results[backend]["ok"] &= restarted["ok"]
                if restarted["ok"]:
                    context = camera._read_lease(timeout=1)
                    lease = context.__enter__()
                    if lease is None:
                        context.__exit__(None, None, None)
                        raise RuntimeError(
                            "No frame available for retained-lease check"
                        )
                    pixels = camera._process_stage(
                        stage=lease.stage,
                        frame_width=lease.frame_width,
                        frame_height=lease.frame_height,
                        rotation_angle=lease.rotation_angle,
                    )
                    retained[backend] = (context, lease, pixels)
            print(
                json.dumps(
                    {
                        "event": "checked",
                        "epoch": spec["epoch"],
                        "ok": all(r["ok"] for r in results.values()),
                        "backends": results,
                        "wgc_sizes": list(wgc_sizes),
                    }
                ),
                flush=True,
            )
    finally:
        for backend, camera in cameras.items():
            try:
                release_retained(backend)
            finally:
                camera.release()
        WinRTDuplicator._frame_size_mismatch = original_mismatch


class Child:
    def __init__(self, arguments):
        self.process = subprocess.Popen(
            [sys.executable, "-I", *map(str, arguments)],
            cwd=ROOT,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.messages = queue.Queue()
        self.errors = []

        def read_stdout():
            for line in self.process.stdout:
                try:
                    self.messages.put(json.loads(line))
                except ValueError:
                    self.errors.append(line)
            self.messages.put(None)

        def read_stderr():
            for line in self.process.stderr:
                self.errors.append(line)

        threading.Thread(target=read_stdout, daemon=True).start()
        threading.Thread(target=read_stderr, daemon=True).start()

    def receive(self, timeout=40):
        try:
            message = self.messages.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError(
                "Child did not reply: " + "".join(self.errors)[-3000:]
            ) from None
        if message is None:
            raise RuntimeError("Child exited: " + "".join(self.errors)[-6000:])
        return message

    def request(self, command, timeout=40):
        self.process.stdin.write(json.dumps(command) + "\n")
        self.process.stdin.flush()
        return self.receive(timeout)

    def close(self):
        if self.process.poll() is None:
            try:
                self.process.stdin.write('{"stop":true}\n')
                self.process.stdin.flush()
                self.process.wait(timeout=15)
            except (OSError, subprocess.TimeoutExpired):
                self.process.kill()
                self.process.wait(timeout=5)
        return {
            "returncode": self.process.returncode,
            "stderr": "".join(self.errors)[-6000:],
        }


def renderer_command(mode, epoch):
    return {k: mode[k] for k in ("x", "y", "width", "height")} | {"epoch": epoch}


def run_case(args, mode, label, target, case_directory):
    from benchmarks.geometry_display import (
        RestoreGuard,
        apply_mode,
        current_mode,
        snapshot_displays,
    )

    snapshot = snapshot_displays()
    renderer = worker = None
    result = {
        "label": label,
        "initial_mode": mode,
        "target_mode": target,
        "checkpoints": [],
        "ok": False,
    }
    guard = RestoreGuard(snapshot, case_directory, timeout_s=120)
    try:
        with guard:
            renderer_args = [
                ROOT / "benchmarks/geometry_pattern.py",
                "--max-seconds",
                "120",
            ]
            for key, value in renderer_command(mode, 1).items():
                renderer_args += ["--" + key, str(value)]
            renderer = Child(renderer_args)
            result["renderer_ready"] = renderer.receive(timeout=10)
            worker = Child(
                [
                    Path(__file__).resolve(),
                    "--worker",
                    "--device-index",
                    args.device_index,
                    "--output-index",
                    args.output_index,
                    "--processor",
                    args.processor,
                    "--backends",
                    *args.backends,
                ]
            )
            result["worker_ready"] = worker.receive(timeout=15)
            result["checkpoints"].append(worker.request(renderer_command(mode, 1)))
            if not result["checkpoints"][-1]["ok"]:
                raise RuntimeError(
                    "Current-mode pixel validation failed; display change skipped"
                )
            if target is not None:
                guard.check()
                result["mode_change_code"] = apply_mode(mode["device_name"], target)
                if result["mode_change_code"] != 0:
                    raise RuntimeError(
                        f"Mode change failed: {result['mode_change_code']}"
                    )
                changed = current_mode(mode["device_name"])
                result["observed_changed_mode"] = changed
                expected_keys = ("width", "height", "orientation")
                if any(changed[k] != target[k] for k in expected_keys):
                    raise RuntimeError(
                        "Actual display mode differs from requested mode"
                    )
                result["renderer_changed"] = renderer.request(
                    renderer_command(changed, 2), timeout=10
                )
                result["checkpoints"].append(
                    worker.request(renderer_command(changed, 2))
                )
                # Restore while capture workers are active, then check a third
                # epoch to distinguish recovery from a cached pre-change image.
                guard.restore()
                if not guard.status["restored"]:
                    raise RuntimeError("Original display mode could not be restored")
                restored = current_mode(mode["device_name"])
                result["renderer_restored"] = renderer.request(
                    renderer_command(restored, 3), timeout=10
                )
                result["checkpoints"].append(
                    worker.request(renderer_command(restored, 3))
                )
            result["ok"] = all(c["ok"] for c in result["checkpoints"])
    except Exception as error:
        result["error"] = repr(error)
    finally:
        if worker is not None:
            result["worker_exit"] = worker.close()
            result["ok"] &= result["worker_exit"]["returncode"] == 0
        if renderer is not None:
            result["renderer_exit"] = renderer.close()
            result["ok"] &= result["renderer_exit"]["returncode"] == 0
        result["restoration"] = guard.status
        result["watchdog_restoration"] = guard.watchdog_status
    return result


def main(args):
    from benchmarks.geometry_display import (
        current_mode,
        enumerate_modes,
        list_displays,
        rotation_mode,
        test_mode,
    )

    output_path = Path(args.output).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    enumeration = Child([Path(__file__).resolve(), "--enumerate"])
    try:
        outputs = enumeration.receive(timeout=15)["outputs"]
    finally:
        enumeration.close()
    selected = next(
        o
        for o in outputs
        if o["device_index"] == args.device_index
        and o["output_index"] == args.output_index
    )
    mode = current_mode(selected["device_name"])
    before = fingerprints()
    payload = {
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "dpi": args.dpi,
        "outputs": outputs,
        "displays": list_displays(),
        "selected": selected,
        "arguments": vars(args),
        "cases": [],
        "skipped": [],
        "fingerprints_start": before,
        "limitations": [
            "Sampled known GDI pixels, not a complete image comparison or HDR test.",
            "No captured images are saved. DPI context is per-monitor-v2 only.",
            "WinRT cursor capture and capture border are disabled in the diagnostic child process.",
            "Live transitions use auto-fullscreen capture; custom ROI checks are stationary.",
            "Capture restarts for ROI checks between transitions, never during an observed transition.",
            "Instrumentation changes timing; these are correctness checks, not performance benchmarks.",
        ],
    }
    payload["git_head"] = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
    ).strip()
    plan = [("current", None)]
    if args.transitions:
        for degrees in (90, 180, 270):
            if degrees == mode["orientation"] * 90:
                continue
            candidate = rotation_mode(mode, degrees)
            code = test_mode(mode["device_name"], candidate)
            if code == 0:
                plan.append((f"rotation_{degrees}", candidate))
            else:
                payload["skipped"].append(
                    {"label": f"rotation_{degrees}", "test_code": code}
                )
        alternatives = [
            m
            for m in enumerate_modes(mode["device_name"])
            if m["orientation"] == mode["orientation"]
            and m["bpp"] == mode["bpp"]
            and 640 <= m["width"] <= mode["width"]
            and 480 <= m["height"] <= mode["height"]
            and (m["width"], m["height"]) != (mode["width"], mode["height"])
        ]
        alternatives.sort(
            key=lambda m: (
                abs(m["frequency"] - mode["frequency"]),
                -(m["width"] * m["height"]),
            )
        )
        for alternative in alternatives:
            candidate = {
                **mode,
                **{k: alternative[k] for k in ("width", "height", "frequency")},
            }
            if test_mode(mode["device_name"], candidate) == 0:
                plan.append(("resize", candidate))
                break
        else:
            payload["skipped"].append(
                {"label": "resize", "reason": "no_supported_smaller_mode"}
            )

    def save():
        payload["fingerprints_end"] = fingerprints()
        payload["sources_unchanged"] = before == payload["fingerprints_end"]
        payload["passed"] = bool(payload["cases"]) and all(
            c["ok"] for c in payload["cases"]
        )
        payload["complete"] = (
            len(payload["cases"]) == len(plan) and not payload["skipped"]
        )
        output_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    save()
    for index, (label, target) in enumerate(plan):
        result = run_case(
            args,
            mode,
            label,
            target,
            output_path.parent / f"restore_{output_path.stem}_{index}",
        )
        payload["cases"].append(result)
        save()
        print(
            json.dumps(
                {"label": label, "ok": result["ok"], "error": result.get("error")}
            ),
            flush=True,
        )
        if not result["ok"]:
            break
    return (
        0
        if payload["passed"] and payload["complete"] and payload["sources_unchanged"]
        else 1
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device-index", type=int, default=0)
    parser.add_argument("--output-index", type=int, default=0)
    parser.add_argument(
        "--processor", choices=("cv2", "numpy", "cython"), default="cv2"
    )
    parser.add_argument(
        "--backends", nargs="+", choices=("dxgi", "winrt"), default=["dxgi", "winrt"]
    )
    parser.add_argument(
        "--transitions",
        action="store_true",
        help="Temporarily rotate/resize the selected display",
    )
    parser.add_argument(
        "--output", default=str(ROOT / "benchmarks/results/native_geometry.json")
    )
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--enumerate", action="store_true", help=argparse.SUPPRESS)
    arguments = parser.parse_args()
    arguments.dpi = set_dpi_awareness()
    faulthandler.dump_traceback_later(110 if arguments.worker else 600, exit=True)
    if arguments.enumerate:
        print(json.dumps({"outputs": enumerate_outputs()}), flush=True)
    elif arguments.worker:
        capture_server(arguments)
    else:
        sys.exit(main(arguments))
