import ctypes
from numpy import rot90, newaxis, uint8, zeros
from numpy.ctypeslib import as_array
from .base import Processor


class NumpyProcessor(Processor):
    def __init__(self, color_mode):
        self.cvtcolor = None
        self.color_mode = color_mode
        self.PBYTE = ctypes.POINTER(ctypes.c_ubyte)

    def process_cvtcolor(self, image):
        import cv2

        # only one time process
        if self.cvtcolor is None:
            color_mapping = {
                "RGB": cv2.COLOR_BGRA2RGB,
                "RGBA": cv2.COLOR_BGRA2RGBA,
                "BGR": cv2.COLOR_BGRA2BGR,
                "GRAY": cv2.COLOR_BGRA2GRAY,
                "BGRA": None,
            }
            cv2_code = color_mapping[self.color_mode]
            if cv2_code is not None:
                if cv2_code != cv2.COLOR_BGRA2GRAY:
                    self.cvtcolor = lambda image: cv2.cvtColor(image, cv2_code)
                else:
                    self.cvtcolor = lambda image: cv2.cvtColor(image, cv2_code)[
                        ..., newaxis
                    ]
            else:
                return image

        return self.cvtcolor(image)

    def shot(self, image_ptr, rect, width, height):
        ctypes.memmove(image_ptr, rect.pBits, width*height*4)

    def process(self, rect, width, height, region, rotation_angle):
        pitch = int(rect.Pitch)
        buffer = ctypes.cast(rect.pBits, self.PBYTE)
        pitch = pitch // 4

        if rotation_angle in (0, 180):
            image = as_array(buffer, (height, pitch, 4))
        elif rotation_angle in (90, 270):
            image = as_array(buffer, (width, pitch, 4))

        if rotation_angle == 90:
            image = rot90(image, axes=(1, 0))
        elif rotation_angle == 180:
            image = rot90(image, k=2, axes=(0, 1))
        elif rotation_angle == 270:
            image = rot90(image, axes=(0, 1))

        if rotation_angle in (0, 180) and pitch != width:
            image = image[:, :width, :]
        elif rotation_angle in (90, 270) and pitch != height:
            image = image[:height, :, :]

        if region[2] - region[0] != width or region[3] - region[1] != height:
            image = image[region[1]:region[3], region[0]:region[2]]

        if self.color_mode is not None:
            return self.process_cvtcolor(image)

        return image
