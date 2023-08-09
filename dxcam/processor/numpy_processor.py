import ctypes
from numpy import rot90, ndarray, newaxis, uint8, zeros
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
        ctypes.memmove(image_ptr, rect.pBits, height*width*4)

    def process(self, rect, width, height, region, rotation_angle):
        width = region[2] - region[0]
        height = region[3] - region[1]
        if rotation_angle in (90, 270):
            width, height = height, width

        buffer = ctypes.cast(rect.pBits, self.PBYTE)
        image = as_array(buffer, (height, width, 4))

        # Another approach from https://github.com/Agade09/DXcam
        # buffer = (ctypes.c_char*height*width*4).from_address(ctypes.addressof(rect.pBits.contents))
        # image = ndarray((height, width, 4), dtype=uint8, buffer=buffer)

        if rotation_angle != 0:
            image = rot90(image, k=rotation_angle//90, axes=(1, 0))

        if self.color_mode is not None:
            return self.process_cvtcolor(image)

        return image
