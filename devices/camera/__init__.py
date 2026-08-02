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
    def is_open(self) -> bool: ...

    @property
    def last_frame_ts(self) -> Optional[float]: ...

    def frame_age_s(self) -> Optional[float]: ...


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
        self._last_frame_ts: Optional[float] = None
        self.opened_at: Optional[float] = None

    def open(self) -> bool:
        self._open = True
        self.opened_at = time.monotonic()
        self._last_frame_ts = None
        return True

    def close(self) -> None:
        self._open = False
        self.opened_at = None
        self._last_frame_ts = None

    def is_open(self) -> bool:
        return bool(self._open)

    @property
    def last_frame_ts(self) -> Optional[float]:
        return self._last_frame_ts

    def frame_age_s(self) -> Optional[float]:
        if self._last_frame_ts is None:
            return None
        return max(0.0, time.monotonic() - self._last_frame_ts)

    def read_bgr(self) -> Optional[np.ndarray]:
        if not self._open:
            return None
        # 简易色块帧，便于 UI/冒烟
        img = np.zeros((240, 320, 3), dtype=np.uint8)
        img[:, :, 1] = 80
        img[60:180, 80:240, 2] = 200
        self._last_frame_ts = time.monotonic()
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
        self._last_frame_ts: Optional[float] = None
        self.opened_at: Optional[float] = None

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
        self.opened_at = time.monotonic()
        self._last_frame_ts = None
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
                    self._last_frame_ts = time.monotonic()
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
            self._last_frame_ts = None
        self.opened_at = None

    def is_open(self) -> bool:
        return self._cap is not None and self._cap.isOpened()

    @property
    def last_frame_ts(self) -> Optional[float]:
        with self._frame_lock:
            return self._last_frame_ts

    def frame_age_s(self) -> Optional[float]:
        with self._frame_lock:
            ts = self._last_frame_ts
        if ts is None:
            return None
        return max(0.0, time.monotonic() - ts)

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


def media_day_dir(data_dir, when=None, *, kind: str = "snapshots"):
    """按日期归档媒体目录：``{data_dir}/{kind}/YYYY-MM-DD/``。

    kind 默认 ``snapshots``（图片）；后续视频可用同一日期目录或 ``kind=\"videos\"``。
    """
    from datetime import datetime
    from pathlib import Path

    base = Path(data_dir)
    day = when if when is not None else datetime.now()
    if hasattr(day, "strftime"):
        day_s = day.strftime("%Y-%m-%d")
    else:
        day_s = str(day)
    folder = (kind or "snapshots").strip() or "snapshots"
    out = base / folder / day_s
    out.mkdir(parents=True, exist_ok=True)
    return out


def snapshot_path(data_dir, filename: str, when=None):
    """返回当日抓拍目录下的完整文件路径（自动建目录）。"""
    from pathlib import Path

    name = Path(filename).name
    return media_day_dir(data_dir, when=when, kind="snapshots") / name


def video_day_dir(data_dir, when=None):
    """按日期归档视频目录：``{data_dir}/videos/YYYY-MM-DD/``（预留）。"""
    return media_day_dir(data_dir, when=when, kind="videos")


def save_snapshot(frame: np.ndarray, path) -> None:
    """保存 BGR 图。Windows 上 cv2.imwrite 对非 ASCII 路径会静默失败，故用 imencode+写文件。"""
    import cv2
    from pathlib import Path

    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    ok, buf = cv2.imencode(".jpg", frame)
    if not ok:
        raise OSError(f"encode jpeg failed: {p}")
    p.write_bytes(buf.tobytes())
    if not p.is_file() or p.stat().st_size <= 0:
        raise OSError(f"snapshot not written: {p}")
