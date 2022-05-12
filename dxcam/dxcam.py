from ctypes import wintypes
import time
import ctypes
from threading import Thread, Event, Lock, Semaphore
import numpy as np
from dxcam.util.timer import (
    WAIT_FAILED,
    create_high_resolution_timer,
    set_periodic_timer,
    wait_for_timer,
    cancel_timer,
    INFINITE,
)
from dxcam.core import Device, Output, StageSurface, Duplicator
from dxcam.processor import Processor


class DXCamera:
    def __init__(
        self,
        output: Output,
        device: Device,
        region: tuple[int, int, int, int],
        max_buffer_len=64,
    ) -> None:

        self._device: Device = device
        self._output: Output = output
        self._stagesurf: StageSurface = StageSurface(
            output=self._output, device=self._device
        )
        self._duplicator: Duplicator = Duplicator(
            output=self._output, device=self._device
        )
        self._processor: Processor = Processor()

        self.width, self.height = self._output.resolution
        self.rotation_angle: int = self._output.rotation_angle

        self._region_set_by_user = region is not None
        self.region: tuple[int, int, int, int] = region
        if self.region is None:
            self.region = (0, 0, self.width, self.height)
        self._validate_region(self.region)

        self.max_buffer_len = max_buffer_len
        self.is_capturing = False
        self.__thread = None
        self.__lock = Lock()
        self.__stop_capture = Event()
        # self.__frame_available = Semaphore(value=0)
        self.__frame_available = Event()
        self.__frame_buffer: np.ndarray = None
        self.__head = 0
        self.__tail = 0
        self.__frame_count = 0
        self.__capture_start_time = 0
        self.__timer_handle = None

    def grab(self, region: tuple[int, int, int, int] = None):
        if region is None:
            region = self.region
        self._validate_region(region)
        frame = self._grab(region)
        return frame

    def _grab(self, region: tuple[int, int, int, int]):
        if self._duplicator.update_frame():
            if not self._duplicator.updated:
                return None
            self._device.im_context.CopyResource(
                self._stagesurf.texture, self._duplicator.texture
            )
            self._duplicator.release_frame()
            rect = self._stagesurf.map()
            frame = self._processor.process(
                rect, self.width, self.height, region, self.rotation_angle
            )
            self._stagesurf.unmap()
            self.latest_frame = frame
            return frame
        else:
            time.sleep(0.5)  # Wait for Display mode change (Access Lost)
            self._duplicator.release()
            self._stagesurf.release()
            self._output.update_desc()
            self.width, self.height = self._output.resolution
            if self.region is None or not self._region_set_by_user:
                self.region = (0, 0, self.width, self.height)
            self._validate_region(self.region)
            if self.is_capturing:
                self._rebuild_frame_buffer(self.region)
            self.rotation_angle = self._output.rotation_angle
            self._stagesurf.rebuild(output=self._output, device=self._device)
            self._duplicator = Duplicator(output=self._output, device=self._device)
            return None

    def start(
        self,
        region: tuple[int, int, int, int] = None,
        target_fps: int = 60,
        video_mode=False,
    ):
        if region is None:
            region = self.region
        self._validate_region(region)
        self.is_capturing = True
        frame_shape = (region[3] - region[1], region[2] - region[0], 3)
        self.__frame_buffer = np.ndarray(
            (self.max_buffer_len, *frame_shape), dtype=np.uint8
        )
        self.__thread = Thread(
            target=self.__capture,
            name="DXCamera",
            args=(region, target_fps, video_mode),
        )
        self.__thread.daemon = True
        self.__thread.start()

    def stop(self):
        if self.is_capturing:
            self.__frame_available.set()
            self.__stop_capture.set()
            if self.__thread is not None:
                self.__thread.join(timeout=10)

    def get_latest_frame(self):
        # self.__frame_available.acquire()
        self.__frame_available.wait()
        ret = self.__frame_buffer[(self.__head - 1) % self.max_buffer_len]
        self.__frame_available.clear()
        return ret

    def __capture(
        self, region: tuple[int, int, int, int], target_fps: int = 60, video_mode=False
    ):
        if target_fps != 0:
            period_ms = 1000 // target_fps  # millisenonds for periodic timer
            self.__timer_handle = create_high_resolution_timer()
            set_periodic_timer(self.__timer_handle, period_ms)

        self.__capture_start_time = time.perf_counter()

        capture_error = None

        while not self.__stop_capture.is_set():
            if self.__timer_handle:
                res = wait_for_timer(self.__timer_handle, INFINITE)
                if res == WAIT_FAILED:
                    self.__stop_capture.set()
                    capture_error = ctypes.WinError()
                    continue
            try:
                frame = self._grab(region)
            except Exception as e:
                self.__stop_capture.set()
                capture_error = e
                continue
            if frame is not None:
                with self.__lock:
                    self.__frame_buffer[self.__head] = frame
                    self.__head = (self.__head + 1) % self.max_buffer_len
                    # self.__frame_available.release()
                    self.__frame_available.set()
                    self.__frame_count += 1
            elif video_mode:
                with self.__lock:
                    self.__frame_buffer[self.__head] = np.copy(
                        self.__frame_buffer[(self.__head - 1) % self.max_buffer_len]
                    )
                    self.__head = (self.__head + 1) % self.max_buffer_len
                    # self.__frame_available.release()
                    self.__frame_available.set()
                    self.__frame_count += 1
        if self.__timer_handle:
            cancel_timer(self.__timer_handle)
            self.__timer_handle = None
        if capture_error is not None:
            raise capture_error
        print(
            f"Screen Capture FPS: {int(self.__frame_count/(time.perf_counter() - self.__capture_start_time))}"
        )

    def _rebuild_frame_buffer(self, region):
        if region is None:
            region = self.region
        frame_shape = (region[3] - region[1], region[2] - region[0], 3)
        self.__frame_buffer = np.ndarray(
            (self.max_buffer_len, *frame_shape), dtype=np.uint8
        )
        self.__head = 0
        self.__tail = 0

    def _validate_region(self, region: tuple[int, int, int, int]):
        l, t, r, b = region
        if not (self.width >= r > l >= 0 and self.height >= b > t >= 0):
            raise ValueError(
                f"Invalid Region: Region should be in {self.width}x{self.height}"
            )

    def __del__(self):
        if self.is_capturing:
            self.stop()
        self._duplicator.release()
        self._stagesurf.release()

    def __repr__(self) -> str:
        ret = f"DxOutputDuplicator:\n"
        ret += f"\tDevice:\t {self._device}"
        ret += f"\tOutput:\t {self._output}"
        ret += f"\tProcessor:\t {self._processor}"
        return ret
