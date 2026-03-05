import ctypes
import numpy as np
from .base import Processor


class NumpyProcessor(Processor):
    def __init__(self, color_mode):
        self.cvtcolor = None
        self.color_mode = color_mode
        if self.color_mode=='BGRA':
            self.color_mode = None

    def process_cvtcolor(self, image):
        import cv2

        # only one time process
        if self.cvtcolor is None:
            color_mapping = {
                "RGB": cv2.COLOR_BGRA2RGB,
                "RGBA": cv2.COLOR_BGRA2RGBA,
                "BGR": cv2.COLOR_BGRA2BGR,
                "GRAY": cv2.COLOR_BGRA2GRAY
            }
            cv2_code = color_mapping[self.color_mode]
            if cv2_code != cv2.COLOR_BGRA2GRAY:
                self.cvtcolor = lambda image: cv2.cvtColor(image, cv2_code)
            else:
                self.cvtcolor = lambda image: cv2.cvtColor(image, cv2_code)[
                    ..., np.newaxis
                ] 
        return self.cvtcolor(image)

    def process(self, rect, width, height, region, rotation_angle):
        pitch = int(rect.Pitch)

        if rotation_angle in (0, 180):
            offset = (region[1] if rotation_angle==0 else height-region[3])*pitch
            height = region[3] - region[1]
        else:
            offset = (region[0] if rotation_angle==270 else width-region[2])*pitch
            width = region[2] - region[0]

        if rotation_angle in (0, 180):
            size = pitch * height
        else:
            size = pitch * width

        buffer = (ctypes.c_char*size).from_address(ctypes.addressof(rect.pBits.contents)+offset)#Pointer arithmetic
        pitch = pitch // 4
        if rotation_angle in (0, 180):
            image = np.ndarray((height, pitch, 4), dtype=np.uint8, buffer=buffer)
        elif rotation_angle in (90, 270):
            image = np.ndarray((width, pitch, 4), dtype=np.uint8, buffer=buffer)

        if not self.color_mode is None:
            image = self.process_cvtcolor(image)

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

        if region[3] - region[1] != image.shape[0]:
            image = image[region[1] : region[3], :, :]
        if region[2] - region[0] != image.shape[1]:
            image = image[:, region[0] : region[2], :]

        return image
