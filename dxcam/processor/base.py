import enum


class ProcessorBackends(enum.Enum):
    PIL = 0
    NUMPY = 1


class Processor:
    def __init__(self, backend=ProcessorBackends.NUMPY, output_color: str = "RGB"):
        self.color_mode = output_color
        self.backend = self._initialize_backend(backend)

    def process(self, rect, width, height, region, rotation_angle):
        return self.backend.process(rect, width, height, region, rotation_angle)

    def process2(self, image_ptr, rect, width, height):
        self.backend.shot(image_ptr, rect, width, height)

    def _initialize_backend(self, backend):
        if backend == ProcessorBackends.NUMPY:
            from dxcam.processor.numpy_processor import NumpyProcessor

            return NumpyProcessor(self.color_mode)
