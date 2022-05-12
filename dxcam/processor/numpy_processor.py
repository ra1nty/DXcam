import ctypes
import numpy as np
import cv2
from .base import Processor


class NumpyProcessor(Processor):

    color_mapping = {
        "RGB": cv2.COLOR_BGRA2RGB,
        "RGBA": cv2.COLOR_BGRA2RGBA,
        "BGR": cv2.COLOR_BGRA2BGR,
        "GRAY": cv2.COLOR_BGRA2GRAY,
        "BGRA": None,
    }

    def __init__(self, color_mode):
        cv2_code = self.color_mapping[color_mode]
        self.cvtcolor = None
        if cv2_code is not None:
            self.cvtcolor = lambda image: cv2.cvtColor(image, cv2_code)

    def process(self, rect, width, height, region, rotation_angle):
        pitch = int(rect.Pitch)

        if rotation_angle in (0, 180):
            size = pitch * height
        else:
            size = pitch * width

        buffer = ctypes.string_at(rect.pBits, size)
        pitch = pitch // 4
        if rotation_angle in (0, 180):
            image = np.ndarray((height, pitch, 4), dtype=np.uint8, buffer=buffer)
        elif rotation_angle in (90, 270):
            image = np.ndarray((width, pitch, 4), dtype=np.uint8, buffer=buffer)

        if self.cvtcolor is not None:
            image = self.cvtcolor(image)

        if rotation_angle == 90:
            image = np.rot90(image, axes=(1, 0))
        elif rotation_angle == 180:
            image = np.rot90(image, k=2, axes=(0, 1))
        elif rotation_angle == 270:
            image = np.rot90(image, axes=(0, 1))

        if rotation_angle in (0, 180) and pitch != width:
            image = image[:, :width, :]
        elif rotation_angle in (90, 270) and pitch != height:
            image = image[:height, :, :]

        if region[2] - region[0] != width or region[3] - region[1] != height:
            image = image[region[1] : region[3], region[0] : region[2], :]

        return image
