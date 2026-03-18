from __future__ import annotations

import argparse
import queue
import threading
import time
from dataclasses import dataclass
from importlib import import_module
from typing import Any, cast

import dxcam

from dxcam.types import CaptureBackend, ProcessorBackend, Region


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Record a video with DXcam using a background writer thread."
    )
    parser.add_argument(
        "--capture-backend",
        choices=("dxgi", "winrt"),
        default="dxgi",
        help="Capture backend (default: dxgi).",
    )
    parser.add_argument(
        "--processor-backend",
        choices=("cv2", "cython", "numpy"),
        default="cv2",
        help="Processor backend (default: cv2).",
    )
    parser.add_argument(
        "--fps",
        type=int,
        default=60,
        help="Target capture/video FPS (default: 60).",
    )
    parser.add_argument(
        "--frames",
        type=int,
        default=600,
        help="How many frames to capture (default: 600).",
    )
    parser.add_argument(
        "--region",
        type=int,
        nargs=4,
        metavar=("LEFT", "TOP", "RIGHT", "BOTTOM"),
        default=None,
        help="Capture region. Defaults to full selected output.",
    )
    parser.add_argument(
        "--codec",
        default="mp4v",
        help=(
            "FourCC codec (default: mp4v). "
            "Use 'uncompressed' or 'raw' for uncompressed AVI."
        ),
    )
    parser.add_argument(
        "--output",
        default="video.mp4",
        help="Output video file path (default: video.mp4).",
    )
    return parser.parse_args()


def _resolve_region(camera: dxcam.DXCamera, region_arg: list[int] | None) -> Region:
    if region_arg is None:
        return (0, 0, camera.width, camera.height)
    left, top, right, bottom = region_arg
    if not (0 <= left < right <= camera.width and 0 <= top < bottom <= camera.height):
        raise ValueError(
            f"Invalid region {(left, top, right, bottom)} for output size "
            f"{camera.width}x{camera.height}."
        )
    return (left, top, right, bottom)


def _make_writer(
    cv2_mod: Any,
    output_path: str,
    codec: str,
    fps: int,
    width: int,
    height: int,
) -> Any:
    codec_normalized = codec.strip().lower()
    if codec_normalized in ("uncompressed", "raw"):
        if not output_path.lower().endswith(".avi"):
            raise ValueError(
                "Uncompressed mode requires an AVI output path, e.g. '--output out.avi'."
            )
        fourcc = 0
    else:
        if len(codec) != 4:
            raise ValueError(
                "Codec/FourCC must be 4 chars (e.g. 'mp4v') or 'uncompressed'."
            )
        fourcc = cv2_mod.VideoWriter_fourcc(*codec)

    writer = cv2_mod.VideoWriter(output_path, fourcc, fps, (width, height))
    if not writer.isOpened():
        raise RuntimeError(
            f"Failed to open VideoWriter for '{output_path}' with codec '{codec}'."
        )
    return writer


def _enqueue_latest(
    frame_queue: queue.Queue[Any],
    frame: Any,
) -> int:
    """Drop oldest when queue is full to keep latency bounded."""
    try:
        frame_queue.put_nowait(frame)
        return 0
    except queue.Full:
        try:
            frame_queue.get_nowait()
        except queue.Empty:
            return 1
        try:
            frame_queue.put_nowait(frame)
            return 1
        except queue.Full:
            return 1


@dataclass
class _WriterStats:
    written_frames: int = 0


def _writer_worker(
    frame_queue: queue.Queue[Any],
    writer: Any,
    stats: _WriterStats,
) -> None:
    try:
        while True:
            frame = frame_queue.get()
            if frame is None:
                return
            writer.write(frame)
            stats.written_frames += 1
    finally:
        writer.release()


def _signal_writer_stop(frame_queue: queue.Queue[Any]) -> None:
    while True:
        try:
            frame_queue.put_nowait(None)
            return
        except queue.Full:
            try:
                frame_queue.get_nowait()
            except queue.Empty:
                continue


def main() -> None:
    # Install OpenCV using: `pip install "dxcam[cv2]"`.
    cv2 = cast(Any, import_module("cv2"))
    args = parse_args()
    if args.fps <= 0:
        raise ValueError("--fps must be > 0.")
    if args.frames <= 0:
        raise ValueError("--frames must be > 0.")

    capture_backend = cast(CaptureBackend, args.capture_backend)
    processor_backend = cast(ProcessorBackend, args.processor_backend)
    queue_size = max(4, args.fps)

    camera = dxcam.create(
        output_color="BGR",
        backend=capture_backend,
        processor_backend=processor_backend,
    )

    writer_thread: threading.Thread | None = None
    frame_queue: queue.Queue[Any] | None = None
    writer_stats = _WriterStats()
    dropped_frames = 0
    captured_frames = 0
    started = time.perf_counter()
    region: Region = (0, 0, camera.width, camera.height)
    try:
        region = _resolve_region(camera=camera, region_arg=args.region)
        width = region[2] - region[0]
        height = region[3] - region[1]
        writer = _make_writer(
            cv2_mod=cv2,
            output_path=args.output,
            codec=args.codec,
            fps=args.fps,
            width=width,
            height=height,
        )

        frame_queue = queue.Queue(maxsize=queue_size)
        writer_thread = threading.Thread(
            target=_writer_worker,
            args=(frame_queue, writer, writer_stats),
            name="DXcamVideoWriter",
            daemon=True,
        )
        writer_thread.start()

        camera.start(region=region, target_fps=args.fps, video_mode=True)
        print(
            "Recording started: "
            f"backend={capture_backend} processor={processor_backend} "
            f"fps={args.fps} frames={args.frames} region={region} "
            f"queue={queue_size} output={args.output}"
        )
        while captured_frames < args.frames:
            frame = camera.get_latest_frame()
            if frame is None:
                continue
            dropped_frames += _enqueue_latest(frame_queue=frame_queue, frame=frame)
            captured_frames += 1
    finally:
        try:
            camera.stop()
        finally:
            if frame_queue is not None:
                _signal_writer_stop(frame_queue)
            if writer_thread is not None:
                writer_thread.join(timeout=10)
            camera.release()

    elapsed = time.perf_counter() - started
    capture_fps = captured_frames / elapsed if elapsed > 0 else 0.0
    write_fps = writer_stats.written_frames / elapsed if elapsed > 0 else 0.0
    drop_ratio = dropped_frames / captured_frames if captured_frames > 0 else 0.0
    print(
        "Recording finished: "
        f"captured={captured_frames} written={writer_stats.written_frames} "
        f"dropped={dropped_frames} drop_ratio={drop_ratio:.2%} "
        f"capture_fps={capture_fps:.2f} write_fps={write_fps:.2f} "
        f"output={args.output}"
    )


if __name__ == "__main__":
    main()
