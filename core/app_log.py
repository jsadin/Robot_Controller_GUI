"""统一应用日志：落盘 + 内存环形缓冲 + 导出 zip。"""

from __future__ import annotations

import json
import logging
import os
import time
import zipfile
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional

_RING_MAX = 2000
_MAX_BYTES = 2 * 1024 * 1024
_BACKUP_COUNT = 3

_ring: Deque["LogEntry"] = deque(maxlen=_RING_MAX)
_setup_done = False
_logger: Optional[logging.Logger] = None


@dataclass
class LogEntry:
    ts: float
    level: str
    source: str
    message: str

    def line(self) -> str:
        t = datetime.fromtimestamp(self.ts).strftime("%Y-%m-%d %H:%M:%S")
        return f"{t} [{self.level}] [{self.source}] {self.message}"


def data_dir() -> Path:
    return Path(os.path.expanduser("~")) / ".robot_controller"


def app_log_path() -> Path:
    return data_dir() / "app.log"


def crash_log_path() -> Path:
    return data_dir() / "crash.log"


def setup_logging() -> None:
    """初始化 RotatingFileHandler；可重复调用（幂等）。"""
    global _setup_done, _logger
    if _setup_done:
        return
    d = data_dir()
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    logger = logging.getLogger("robot_controller")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not any(isinstance(h, RotatingFileHandler) for h in logger.handlers):
        try:
            fh = RotatingFileHandler(
                str(app_log_path()),
                maxBytes=_MAX_BYTES,
                backupCount=_BACKUP_COUNT,
                encoding="utf-8",
            )
            fh.setFormatter(
                logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
            )
            logger.addHandler(fh)
        except OSError:
            pass
    _logger = logger
    _setup_done = True
    log_info("app", "logging started")


def _append(level: str, source: str, message: str) -> None:
    entry = LogEntry(time.time(), level, source or "app", str(message))
    _ring.append(entry)
    if _logger is None:
        return
    text = f"[{entry.source}] {entry.message}"
    if level == "ERROR":
        _logger.error(text)
    elif level == "WARN":
        _logger.warning(text)
    else:
        _logger.info(text)


def log_info(source: str, message: str) -> None:
    _append("INFO", source, message)


def log_warn(source: str, message: str) -> None:
    _append("WARN", source, message)


def log_error(source: str, message: str) -> None:
    _append("ERROR", source, message)


def get_recent(n: int = 200) -> List[LogEntry]:
    if n <= 0:
        return list(_ring)
    items = list(_ring)
    return items[-n:]


def export_bundle(path: str | Path, snapshot: Optional[Dict[str, Any]] = None) -> Path:
    """打包 app.log / crash.log / diagnosis_snapshot.json / status_tail.txt。"""
    setup_logging()
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        app_p = app_log_path()
        if app_p.is_file():
            zf.write(app_p, arcname="app.log")
        crash_p = crash_log_path()
        if crash_p.is_file():
            zf.write(crash_p, arcname="crash.log")
        snap = snapshot if snapshot is not None else {}
        zf.writestr(
            "diagnosis_snapshot.json",
            json.dumps(snap, ensure_ascii=False, indent=2, default=str),
        )
        tail = "\n".join(e.line() for e in _ring)
        zf.writestr("status_tail.txt", tail + ("\n" if tail else ""))
    return out
