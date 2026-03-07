"""Minimal instant-replay example using DXcam + PyAV + pynput hotkeys."""

from __future__ import annotations

from collections import deque
from importlib import import_module
import logging
from threading import Event, Lock
from typing import Any

import dxcam

TARGET_FPS = 120
SECONDS_TO_KEEP = 10
BIT_RATE = 8_000_000
WIDTH = 1920
HEIGHT = 1080

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

stop_event = Event()
buffer_lock = Lock()
replay_count = 0
buffer: deque[Any] = deque(maxlen=TARGET_FPS * SECONDS_TO_KEEP)

# Optional runtime dependencies are loaded dynamically so this module
# remains importable without pyav/pynput installed.
av = import_module("av")
keyboard = import_module("pynput.keyboard")

container = av.open(f"replay{replay_count}.mp4", mode="w")
stream = container.add_stream("mpeg4", rate=TARGET_FPS)
stream.pix_fmt, stream.height, stream.width = "yuv420p", HEIGHT, WIDTH
stream.bit_rate = BIT_RATE

camera = dxcam.create(output_color="RGB")
camera.start(target_fps=TARGET_FPS, video_mode=True)
logger.info(
    "Instant replay started. target_fps=%d window=%ds resolution=%dx%d",
    TARGET_FPS,
    SECONDS_TO_KEEP,
    WIDTH,
    HEIGHT,
)


def save_replay() -> None:
    """Write the buffered packets to disk and roll to a new output file."""
    global container, stream, replay_count

    logger.info("Saving instant replay for the last %d seconds.", SECONDS_TO_KEEP)
    with buffer_lock:
        logger.debug("Muxing %d buffered packets.", len(buffer))
        for idx, packet in enumerate(buffer):
            packet.pts = packet.dts = idx
            container.mux(packet)
    for packet in stream.encode():
        container.mux(packet)
    container.close()

    replay_count += 1
    container = av.open(f"replay{replay_count}.mp4", mode="w")
    stream = container.add_stream("mpeg4", rate=TARGET_FPS)
    stream.pix_fmt, stream.height, stream.width = "yuv420p", HEIGHT, WIDTH
    stream.bit_rate = BIT_RATE


def stop_record() -> None:
    """Signal the recorder loop to stop."""
    logger.info("Stopping instant replay.")
    stop_event.set()


def main() -> None:
    """Run the hotkey listener and packet capture loop."""
    logger.info("Hotkeys: save=<ctrl>+<alt>+h stop=<ctrl>+<alt>+i")
    listener = keyboard.GlobalHotKeys(
        {"<ctrl>+<alt>+h": save_replay, "<ctrl>+<alt>+i": stop_record}
    )
    listener.start()
    encoded_packets = 0
    try:
        listener.wait()
        while not stop_event.is_set():
            frame = camera.get_latest_frame()
            if frame is None:
                continue
            video_frame = av.VideoFrame.from_ndarray(frame, format="rgb24")
            with buffer_lock:
                for packet in stream.encode(video_frame):
                    buffer.append(packet)
                    encoded_packets += 1
                    if encoded_packets % TARGET_FPS == 0:
                        logger.debug(
                            "Buffered packets=%d current_buffer=%d",
                            encoded_packets,
                            len(buffer),
                        )
    finally:
        listener.stop()
        listener.join()
        camera.stop()
        container.close()
        logger.info("Instant replay exited cleanly.")


if __name__ == "__main__":
    main()
