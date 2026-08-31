"""海康 IPC ISAPI（HTTP Digest）：电动变焦。

产线变焦机（如 DS-2CD4B04/60-IZ）无云台，变倍走 PTZ 通道的 zoom。
不依赖 HCNetSDK，与现有 OpenCV RTSP 预览并行。
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from typing import Optional

from devices.config_loader import CameraCfg

try:
    import requests
    from requests.auth import HTTPDigestAuth
except ImportError:  # pragma: no cover
    requests = None  # type: ignore
    HTTPDigestAuth = None  # type: ignore


def _ptz_xml(*, pan: int = 0, tilt: int = 0, zoom: int = 0) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<PTZData>"
        f"<pan>{int(pan)}</pan>"
        f"<tilt>{int(tilt)}</tilt>"
        f"<zoom>{int(zoom)}</zoom>"
        "</PTZData>"
    )


def _clamp_speed(v: int) -> int:
    if v > 100:
        return 100
    if v < -100:
        return -100
    return int(v)


def _xml_flag(text: str, tag: str) -> Optional[bool]:
    m = re.search(rf"<{tag}>\s*(true|false)\s*</{tag}>", text, re.I)
    if not m:
        return None
    return m.group(1).lower() == "true"


@dataclass
class PtzCaps:
    probed: bool = False
    zoom: bool = True
    detail: str = ""


class HikvisionIsapi:
    """复用 Session（Digest 只握手一次），变焦 PUT 在后台线程，避免卡住 Qt。"""

    def __init__(self, cfg: CameraCfg) -> None:
        self._cfg = cfg
        self._lock = threading.Lock()
        self._last_error: str = ""
        self.caps = PtzCaps()
        self._session = None
        if requests is not None:
            self._session = requests.Session()

    @property
    def last_error(self) -> str:
        return self._last_error

    def available(self) -> bool:
        if requests is None:
            return False
        kind = (self._cfg.kind or "").strip().lower()
        if kind not in ("hikvision", "rtsp"):
            return False
        return bool((self._cfg.host or "").strip())

    def zoom_start(self, direction: int) -> bool:
        """direction: +1 放大, -1 缩小。"""
        if not self.available():
            self._last_error = "当前相机类型不支持 ISAPI 变焦"
            return False
        if self.caps.probed and not self.caps.zoom:
            self._last_error = "本机不支持电动变焦"
            return False
        speed = abs(int(self._cfg.zoom_speed or 50)) or 50
        z = _clamp_speed(speed if direction >= 0 else -speed)
        self._put_async(self._ptz_continuous_path(), _ptz_xml(zoom=z))
        return True

    def stop(self) -> None:
        if not self.available():
            return
        self._put_async(self._ptz_continuous_path(), _ptz_xml(zoom=0))

    def refresh_capabilities(self) -> PtzCaps:
        caps = PtzCaps(probed=True, zoom=True)
        if not self.available():
            caps.zoom = False
            caps.detail = "ISAPI 不可用"
            self.caps = caps
            return caps
        ptz_xml = self._get(f"/ISAPI/PTZCtrl/channels/{int(self._cfg.ptz_channel or 1)}")
        zoom_ch = _xml_flag(ptz_xml, "zoomSupport")
        if zoom_ch is False:
            caps.zoom = False
            caps.detail = "zoomSupport=false"
        self.caps = caps
        return caps

    def tune_preview_stream(self, channel: int = 102) -> None:
        """子码流缩短 GOP，降低变焦时 H.264 等待关键帧的时间。主码流不改。"""
        if not self.available():
            return
        path = f"/ISAPI/Streaming/channels/{int(channel)}"
        xml = self._get(path)
        if not xml:
            return
        m = re.search(r"<GovLength>(\d+)</GovLength>", xml)
        if m and int(m.group(1)) <= 12:
            return
        xml2 = re.sub(r"<GovLength>\d+</GovLength>", "<GovLength>10</GovLength>", xml)
        xml2 = re.sub(
            r"<keyFrameInterval>\d+</keyFrameInterval>",
            "<keyFrameInterval>400</keyFrameInterval>",
            xml2,
        )
        if xml2 == xml:
            return
        self._put(path, xml2)

    def get_picture(self, channel: int = 101) -> Optional[bytes]:
        """主码流抓图 JPEG（不走 RTSP，避免预览用 1440p）。"""
        if not self.available() or requests is None:
            return None
        path = f"/ISAPI/Streaming/channels/{int(channel)}/picture"
        try:
            r = self._request("GET", path, timeout=4.0)
            if r is not None and r.status_code == 200 and r.content:
                return bytes(r.content)
        except Exception as e:
            self._last_error = str(e)
        return None

    def _base(self) -> str:
        host = (self._cfg.host or "").strip()
        port = int(self._cfg.http_port or 80)
        if port == 80:
            return f"http://{host}"
        return f"http://{host}:{port}"

    def _ptz_continuous_path(self) -> str:
        ch = int(self._cfg.ptz_channel or 1)
        return f"/ISAPI/PTZCtrl/channels/{ch}/continuous"

    def _auth(self):
        user = self._cfg.user or "admin"
        password = self._cfg.password or ""
        return HTTPDigestAuth(user, password)

    def _put_async(self, path: str, body: str) -> None:
        t = threading.Thread(
            target=self._put, args=(path, body), name="hik-isapi", daemon=True
        )
        t.start()

    def _get(self, path: str) -> str:
        try:
            r = self._request("GET", path, timeout=3.0)
            if r is not None and r.status_code == 200:
                return r.text or ""
        except Exception:
            return ""
        return ""

    def _put(self, path: str, body: str) -> None:
        if not self.available():
            self._last_error = "当前相机类型不支持 ISAPI 变焦"
            return
        try:
            r = self._request(
                "PUT",
                path,
                data=body.encode("utf-8") if body else None,
                headers={"Content-Type": "application/xml; charset=UTF-8"},
                timeout=3.0,
            )
            if r is None:
                self._last_error = "ISAPI 无响应"
                return
            if r.status_code == 401:
                r = self._request(
                    "PUT",
                    path,
                    data=body.encode("utf-8") if body else None,
                    headers={"Content-Type": "application/xml; charset=UTF-8"},
                    timeout=3.0,
                    basic=True,
                )
            if r is None or r.status_code >= 400:
                snippet = ((r.text if r is not None else "") or "")[:180].replace("\n", " ")
                self._last_error = f"HTTP {getattr(r, 'status_code', '?')} {path} {snippet}"
            else:
                self._last_error = ""
        except Exception as e:
            self._last_error = str(e)

    def _request(
        self,
        method: str,
        path: str,
        *,
        data: Optional[bytes] = None,
        headers: Optional[dict] = None,
        timeout: float = 3.0,
        basic: bool = False,
    ):
        if requests is None or HTTPDigestAuth is None or self._session is None:
            self._last_error = "requests 未安装"
            return None
        url = self._base() + path
        user = self._cfg.user or "admin"
        password = self._cfg.password or ""
        auth = (user, password) if basic else self._auth()
        with self._lock:
            return self._session.request(
                method,
                url,
                data=data,
                auth=auth,
                headers=headers,
                timeout=timeout,
            )
