import argparse
import statistics
import time

import dxcam
from dxcam.types import CaptureBackend, ProcessorBackend


def run_capture_pass(
    capture_backend: CaptureBackend,
    processor_backend: ProcessorBackend,
    target_fps: int,
    target_frames: int,
) -> dict[str, float]:
    camera = dxcam.create(
        output_idx=0,
        backend=capture_backend,
        processor_backend=processor_backend,
        output_color="BGRA",
    )
    camera.start(target_fps=target_fps, video_mode=False)
    start = time.perf_counter()
    timestamps: list[float] = []
    non_none = 0
    for _ in range(target_frames):
        result = camera.get_latest_frame(with_timestamp=True)
        if result is None:
            continue
        _frame, ts = result
        non_none += 1
        timestamps.append(ts)
    elapsed = time.perf_counter() - start
    camera.stop()
    camera.release()

    if len(timestamps) < 2:
        mean_delta = 0.0
        jitter = 0.0
    else:
        deltas = [b - a for a, b in zip(timestamps, timestamps[1:])]
        mean_delta = statistics.fmean(deltas)
        jitter = statistics.pstdev(deltas) if len(deltas) > 1 else 0.0

    return {
        "elapsed_s": elapsed,
        "non_none": float(non_none),
        "reads_per_s": float(target_frames) / elapsed if elapsed > 0 else 0.0,
        "frames_per_s": float(non_none) / elapsed if elapsed > 0 else 0.0,
        "mean_delta_s": mean_delta,
        "jitter_s": jitter,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare DXcam cv2 vs numpy processor backends."
    )
    parser.add_argument(
        "--backend",
        choices=("dxgi", "winrt"),
        default="dxgi",
        help="Capture backend.",
    )
    parser.add_argument(
        "--target-fps",
        type=int,
        default=120,
        help="Capture target FPS.",
    )
    parser.add_argument(
        "--target-frames",
        type=int,
        default=1000,
        help="Benchmark loop iterations.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    print(
        f"Running capture backend={args.backend}, target_fps={args.target_fps}, "
        f"target_frames={args.target_frames}"
    )
    for processor_backend in ("cv2", "numpy"):
        metrics = run_capture_pass(
            capture_backend=args.backend,
            processor_backend=processor_backend,
            target_fps=args.target_fps,
            target_frames=args.target_frames,
        )
        print(
            f"[{processor_backend}] elapsed={metrics['elapsed_s']:.3f}s "
            f"reads/s={metrics['reads_per_s']:.2f} "
            f"frames/s={metrics['frames_per_s']:.2f} "
            f"non_none={int(metrics['non_none'])} "
            f"mean_delta={metrics['mean_delta_s']:.6f}s "
            f"jitter={metrics['jitter_s']:.6f}s"
        )


if __name__ == "__main__":
    main()
