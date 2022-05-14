"""A Minimal example of creating a ghetto instant replay with hotkeys.
Took less than 5 minutes to write using pyav + dxcam + pynput.
The code is shit but you got the idea.
"""
from collections import deque
from threading import Event, Lock
import dxcam
import av
from pynput import keyboard


stop_event = Event()
buffer_lock = Lock()
replay_count = 0
target_fps = 120
buffer = deque(maxlen=target_fps * 10)

container = av.open(f"replay{replay_count}.mp4", mode="w")
stream = container.add_stream("mpeg4", rate=target_fps)
stream.pix_fmt, stream.height, stream.width = "yuv420p", 1080, 1920
stream.bit_rate = 8_000_000

camera = dxcam.create(output_color="RGB")
camera.start(target_fps=target_fps, video_mode=True)


def save_replay():
    global container, buffer_lock, stream, buffer, replay_count
    print("Saving Instant Replay for the last 10 seconds...")
    with buffer_lock:
        for idx, packet in enumerate(buffer):
            packet.pts = packet.dts = idx
            container.mux(packet)
    for packet in stream.encode():
        container.mux(packet)
    container.close()
    replay_count += 1
    container = av.open(f"replay{replay_count}.mp4", mode="w")
    stream = container.add_stream("mpeg4", rate=120)
    stream.pix_fmt, stream.height, stream.width = "yuv420p", 1080, 1920
    stream.bit_rate = 8_000_000


def stop_record():
    global stop_event
    print("Closing")
    stop_event.set()


listener = keyboard.GlobalHotKeys(
    {"<ctrl>+<alt>+h": save_replay, "<ctrl>+<alt>+i": stop_record}
)
listener.start()
try:
    listener.wait()
    while not stop_event.is_set():
        frame = av.VideoFrame.from_ndarray(camera.get_latest_frame(), format="rgb24")
        try:
            with buffer_lock:
                for packet in stream.encode(frame):
                    buffer.append(packet)
            del frame
        except Exception as e:
            continue
finally:
    listener.stop()
listener.join()
camera.stop()
container.close()
