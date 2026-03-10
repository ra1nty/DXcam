from __future__ import annotations

from importlib import import_module
from typing import Any, cast

import dxcam


def main() -> None:
    """Show one full-screen screenshot from the primary output."""
    # Install OpenCV using: `pip install "dxcam[cv2]"`
    cv2 = cast(Any, import_module("cv2"))

    camera = dxcam.create(
        output_color="BGR", output_idx=0
    )  # primary output on device 0
    try:
        frame = camera.grab()

        if frame is None:
            raise RuntimeError("No new frame was available from primary output.")

        cv2.imshow("DXcam Primary Output Screenshot", frame)
        cv2.waitKey(0)
    finally:
        camera.release()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
