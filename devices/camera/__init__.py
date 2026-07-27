"""摄像头后端（OpenCV / Mock）。"""

from __future__ import annotations

import os
import threading
import time
from typing import Optional, Protocol
from urllib.parse import quote

import numpy as np

from devices.config_loader import CameraCfg, DevicesConfig


class CameraBackend(Protocol):
    def open(self) -> bool: ...
    def close(self) -> None: ...
    def read_bgr(self) -> Optional[np.ndarray]: ...


def build_hikvision_rtsp_url(
    host: str,
    *,
    user: str = "admin",
    password: str = "",
    port: int = 554,
    stream_path: str = "/h264/ch1/main/av_stream",
) -> str:
    h = host.strip()
    if not h:
        raise ValueError("hikvision host is empty")
    path = stream_path.strip() or "/h264/ch1/main/av_stream"
    if not path.startswith("/"):
        path = "/" + path
    return f"rtsp://{quote(user, safe='')}:{quote(password, safe='')}@{h}:{int(port)}{path}"


class MockCameraBackend:
    def __init__(self) -> None:
        self._open = False

    def open(self) -> bool:
        self._open = True
        return True

    def close(self) -> None:
        self._open = False

    def read_bgr(self) -> Optional[np.ndarray]:
        if not self._open:
            return None
        # 简易色块帧，便于 UI/冒烟
        img = np.zeros((240, 320, 3), dtype=np.uint8)
        img[:, :, 1] = 80
        img[60:180, 80:240, 2] = 200
        return img


_RTSP_FFMPEG_OPTS = "rtsp_transport;tcp|fflags;nobuffer|max_delay;500000"


class OpenCvCameraBackend:
    def __init__(self, cfg: CameraCfg) -> None:
        self._cfg = cfg
        self._cap = None
        self._using_rtsp = False
        self._reader_thread: threading.Thread | None = None
        self._stop_event: threading.Event | None = None
        self._frame_lock = threading.Lock()
        self._latest_bgr: np.ndarray | None = None

    def _capture_source(self) -> int | str:
        url = (self._cfg.rtsp_url or "").strip()
        if url:
            return url
        kind = (self._cfg.kind or "").lower()
        if kind in ("hikvision", "rtsp") and (self._cfg.host or "").strip():
            return build_hikvision_rtsp_url(
                self._cfg.host,
                user=self._cfg.user,
                password=self._cfg.password,
                port=self._cfg.port,
                stream_path=self._cfg.stream_path,
            )
        return int(self._cfg.usb_index)

    def open(self) -> bool:
        self.close()
        try:
            import cv2
        except ImportError:
            return False
        src = self._capture_source()
        self._using_rtsp = isinstance(src, str) and str(src).lower().startswith("rtsp://")
        if self._using_rtsp:
            prev = os.environ.get("OPENCV_FFMPEG_CAPTURE_OPTIONS", "").strip()
            os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = (
                f"{prev}|{_RTSP_FFMPEG_OPTS}" if prev else _RTSP_FFMPEG_OPTS
            )
            self._cap = cv2.VideoCapture(src, cv2.CAP_FFMPEG)
        else:
            self._cap = cv2.VideoCapture(src)
        if not self._cap.isOpened():
            self._cap = None
            return False
        self._stop_event = threading.Event()
        self._reader_thread = threading.Thread(
            target=self._reader_loop, name="camera_reader", daemon=True
        )
        self._reader_thread.start()
        return True

    def _reader_loop(self) -> None:
        assert self._cap is not None and self._stop_event is not None
        while not self._stop_event.is_set():
            if self._cap is None or not self._cap.isOpened():
                break
            ok, frame = self._cap.read()
            if ok and frame is not None:
                with self._frame_lock:
                    self._latest_bgr = frame
            else:
                self._stop_event.wait(0.02)

    def close(self) -> None:
        if self._stop_event is not None:
            self._stop_event.set()
        if self._reader_thread is not None:
            self._reader_thread.join(timeout=3.0)
            self._reader_thread = None
        self._stop_event = None
        if self._cap is not None:
            self._cap.release()
            self._cap = None
        with self._frame_lock:
            self._latest_bgr = None

    def read_bgr(self) -> Optional[np.ndarray]:
        with self._frame_lock:
            if self._latest_bgr is None:
                return None
            return self._latest_bgr.copy()


def build_camera(cfg: DevicesConfig) -> CameraBackend:
    kind = (cfg.camera.kind or "hikvision").lower()
    if kind == "mock":
        return MockCameraBackend()
    return OpenCvCameraBackend(cfg.camera)


def save_snapshot(frame: np.ndarray, path) -> None:
    import cv2
    from pathlib import Path

    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(p), frame)
