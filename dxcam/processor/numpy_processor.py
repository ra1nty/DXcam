import ctypes
import numpy as np
import cv2
from .base import Processor


class NumpyProcessor(Processor):
    def __init__(self):
        pass

    def process(self, rect, width, height, region, rotation_angle):
        pointer = rect.pBits
        pitch = int(rect.Pitch)

        if rotation_angle in (0, 180):
            size = pitch * height
        else:
            size = pitch * width

        image = np.empty((size,), dtype=np.uint8)
        ctypes.memmove(image.ctypes.data, pointer, size)

        pitch = pitch // 4

        if rotation_angle == 0:
            image = cv2.cvtColor(image.reshape(height, pitch, 4), cv2.COLOR_BGRA2RGB)
        elif rotation_angle == 90:
            image = cv2.cvtColor(image.reshape(width, pitch, 4), cv2.COLOR_BGRA2RGB)
            image = np.rot90(image, axes=(1, 0))
        elif rotation_angle == 180:
            image = cv2.cvtColor(image.reshape(height, pitch, 4), cv2.COLOR_BGRA2RGB)
            image = np.rot90(image, k=2, axes=(0, 1))
        elif rotation_angle == 270:
            image = cv2.cvtColor(image.reshape(height, pitch, 4), cv2.COLOR_BGRA2RGB)
            image = np.rot90(image, axes=(0, 1))

        if rotation_angle in (0, 180) and pitch != width:
            image = image[:, :width, :]
        elif rotation_angle in (90, 270) and pitch != height:
            image = image[:height, :, :]

        if region[2] - region[0] != width or region[3] - region[1] != height:
            image = image[region[1] : region[3], region[0] : region[2], :]

        return image
