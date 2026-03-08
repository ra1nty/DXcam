from __future__ import annotations

from importlib import import_module
from typing import Any, cast

import dxcam

from dxcam.types import Region

TOP = (2160 - 640) // 2
LEFT = (3840 - 640) // 2
RIGHT = LEFT + 640
BOTTOM = TOP + 640
TOP = 0
LEFT = 0
RIGHT = 3840
BOTTOM = 2160
REGION: Region = (LEFT, TOP, RIGHT, BOTTOM)
TARGET_FPS = 30
FRAME_COUNT = 600


def main() -> None:
    """Record a short MP4 from DXcam's video-mode capture."""
    # install OpenCV using: `pip install dxcam[cv2]`
    cv2 = cast(Any, import_module("cv2"))

    width = REGION[2] - REGION[0]
    height = REGION[3] - REGION[1]

    camera = dxcam.create(output_idx=0, output_color="BGR")
    writer = cv2.VideoWriter(
        "video.mp4",
        cv2.VideoWriter_fourcc(*"mp4v"),
        TARGET_FPS,
        (width, height),
    )
    try:
        camera.start(region=REGION, target_fps=TARGET_FPS, video_mode=True)
        for _ in range(FRAME_COUNT):
            frame = camera.get_latest_frame()
            if frame is None:
                continue
            writer.write(frame)
    finally:
        camera.stop()
        writer.release()


if __name__ == "__main__":
    main()
