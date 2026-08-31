"""摄像头后端（OpenCV / Mock）。"""

from __future__ import annotations

import os
import threading
import time
from typing import Optional, Protocol
from urllib.parse import quote

import numpy as np

from devices.config_loader import CameraCfg, DevicesConfig
from devices.camera.hikvision_isapi import HikvisionIsapi


class CameraBackend(Protocol):
    def open(self) -> bool: ...
    def close(self) -> None: ...
    def read_bgr(self) -> Optional[np.ndarray]: ...
    def is_open(self) -> bool: ...

    @property
    def last_frame_ts(self) -> Optional[float]: ...

    def frame_age_s(self) -> Optional[float]: ...

    def ptz_available(self) -> bool: ...
    def zoom_start(self, direction: int) -> bool: ...
    def ptz_stop(self) -> bool: ...
    def ptz_last_error(self) -> str: ...
    def last_open_error(self) -> str: ...
    def ptz_caps(self) -> dict: ...
    def refresh_ptz_caps(self) -> dict: ...
    def snapshot_bgr(self) -> Optional[np.ndarray]: ...


def build_hikvision_rtsp_url(
    host: str,
    *,
    user: str = "admin",
    password: str = "",
    port: int = 554,
    stream_path: str = "/Streaming/Channels/102",
) -> str:
    h = host.strip()
    if not h:
        raise ValueError("hikvision host is empty")
    path = stream_path.strip() or "/Streaming/Channels/102"
    if not path.startswith("/"):
        path = "/" + path
    return f"rtsp://{quote(user, safe='')}:{quote(password, safe='')}@{h}:{int(port)}{path}"


def _preview_stream_path(stream_path: str) -> str:
    """预览强制走子码流。主码流 2560×1440 / GOP50 会把变焦画面拖到 1～2 秒。"""
    p = (stream_path or "").strip() or "/Streaming/Channels/102"
    low = p.lower().replace("\\", "/")
    if "main" in low or "/channels/101" in low or low.endswith("/101"):
        return "/Streaming/Channels/102"
    if not p.startswith("/"):
        p = "/" + p
    return p


class MockCameraBackend:
    def __init__(self) -> None:
        self._open = False
        self._last_frame_ts: Optional[float] = None
        self.opened_at: Optional[float] = None

    def ptz_available(self) -> bool:
        return False

    def zoom_start(self, direction: int) -> bool:
        return False

    def ptz_stop(self) -> bool:
        return True

    def ptz_last_error(self) -> str:
        return ""

    def last_open_error(self) -> str:
        return ""

    def ptz_caps(self) -> dict:
        return {"probed": True, "zoom": False, "detail": ""}

    def refresh_ptz_caps(self) -> dict:
        return self.ptz_caps()

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

    def snapshot_bgr(self) -> Optional[np.ndarray]:
        return self.read_bgr()


# OpenCV FFmpeg：低延迟（旧值 max_delay=500ms 会明显拖后变焦画面）
_RTSP_FFMPEG_OPTS = (
    "rtsp_transport;tcp|fflags;nobuffer|flags;low_delay|"
    "max_delay;0|framedrop;1"
)
_PREVIEW_MAX_W = 960


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
        self._last_open_error: str = ""
        self._isapi = HikvisionIsapi(cfg)

    def last_open_error(self) -> str:
        return self._last_open_error

    def ptz_available(self) -> bool:
        return self._isapi.available()

    def zoom_start(self, direction: int) -> bool:
        return bool(self._isapi.zoom_start(direction))

    def ptz_stop(self) -> bool:
        if not self.ptz_available():
            return False
        self._isapi.stop()
        return True

    def ptz_last_error(self) -> str:
        return self._isapi.last_error

    def ptz_caps(self) -> dict:
        c = self._isapi.caps
        return {
            "probed": bool(c.probed),
            "zoom": bool(c.zoom),
            "detail": c.detail or "",
        }

    def refresh_ptz_caps(self) -> dict:
        self._isapi.refresh_capabilities()
        return self.ptz_caps()

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
                stream_path=_preview_stream_path(self._cfg.stream_path),
            )
        return int(self._cfg.usb_index)

    def open(self) -> bool:
        self.close()
        self._last_open_error = ""
        try:
            import cv2
        except ImportError:
            self._last_open_error = "未安装 OpenCV"
            return False
        try:
            self._isapi.tune_preview_stream(102)
        except Exception:
            pass
        src = self._capture_source()
        self._using_rtsp = isinstance(src, str) and str(src).lower().startswith("rtsp://")
        if self._using_rtsp:
            os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = _RTSP_FFMPEG_OPTS
            self._cap = cv2.VideoCapture(src, cv2.CAP_FFMPEG)
            try:
                self._cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            except Exception:
                pass
        else:
            self._cap = cv2.VideoCapture(src)
            try:
                self._cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            except Exception:
                pass
        if not self._cap.isOpened():
            host = (self._cfg.host or "").strip()
            kind = (self._cfg.kind or "").lower()
            if kind in ("hikvision", "rtsp") and host:
                self._last_open_error = (
                    f"RTSP 打开失败 {host}:{int(self._cfg.port)} "
                    f"（请核对 devices.local.yaml 的 camera.host）"
                )
            else:
                self._last_open_error = f"打开失败 source={src!r}"
            self._cap = None
            return False
        self.opened_at = time.monotonic()
        self._last_frame_ts = None
        self._stop_event = threading.Event()
        self._reader_thread = threading.Thread(
            target=self._reader_loop, name="camera_reader", daemon=True
        )
        self._reader_thread.start()
        try:
            self._isapi.refresh_capabilities()
        except Exception:
            pass
        return True

    def _reader_loop(self) -> None:
        assert self._cap is not None and self._stop_event is not None
        try:
            import cv2
        except ImportError:
            return
        while not self._stop_event.is_set():
            if self._cap is None or not self._cap.isOpened():
                break
            ok, frame = self._cap.read()
            if ok and frame is not None:
                h, w = frame.shape[:2]
                if w > _PREVIEW_MAX_W:
                    nh = max(1, int(h * (_PREVIEW_MAX_W / float(w))))
                    frame = cv2.resize(
                        frame, (_PREVIEW_MAX_W, nh), interpolation=cv2.INTER_AREA
                    )
                with self._frame_lock:
                    self._latest_bgr = frame
                    self._last_frame_ts = time.monotonic()
            else:
                self._stop_event.wait(0.01)

    def snapshot_bgr(self) -> Optional[np.ndarray]:
        blob = self._isapi.get_picture(101)
        if blob:
            try:
                import cv2
                arr = np.frombuffer(blob, dtype=np.uint8)
                img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                if img is not None:
                    return img
            except Exception:
                pass
        return self.read_bgr()

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
        try:
            self.ptz_stop()
        except Exception:
            pass

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
