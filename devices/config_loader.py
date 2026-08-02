"""加载 config/devices.yaml（及可选 devices.local.yaml）。"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore


def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False)) or hasattr(sys, "_MEIPASS")


def _bundle_root() -> Path:
    """打包资源根（onefile 解压目录）或源码仓库根。"""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    return Path(__file__).resolve().parents[1]


def _app_dir() -> Path:
    """可写/旁路配置目录：exe 所在目录，或源码仓库根。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def app_dir() -> Path:
    """公开：应用根目录（exe 旁或仓库根）。"""
    return _app_dir()


def bundle_root() -> Path:
    """公开：打包资源根或仓库根。"""
    return _bundle_root()


def _repo_root() -> Path:
    """兼容旧调用：资源查找优先 bundle。"""
    return _bundle_root()


def default_config_path() -> Path:
    env = (os.environ.get("ROBOT_CONFIG") or "").strip()
    if env:
        return Path(env)
    # 现场：优先 exe 旁外部配置，便于改 IP / elite_cs，无需重打包
    for candidate in (
        _app_dir() / "config" / "devices.yaml",
        _app_dir() / "devices.yaml",
    ):
        if candidate.is_file():
            return candidate
    return _bundle_root() / "config" / "devices.yaml"


def _find_local_overlay(primary: Path) -> Optional[Path]:
    """查找 devices.local.yaml（主配置旁 → exe/config → exe 根）。"""
    candidates = [
        primary.with_name("devices.local.yaml"),
        _app_dir() / "config" / "devices.local.yaml",
        _app_dir() / "devices.local.yaml",
    ]
    seen: set[Path] = set()
    for c in candidates:
        try:
            key = c.resolve()
        except OSError:
            key = c
        if key in seen:
            continue
        seen.add(key)
        if c.is_file():
            return c
    return None


def _resolve_rtsi_path(configured: str, filename: str) -> str:
    """解析 RTSI 配方路径；配置为空或文件不存在时回退到打包内置。"""
    configured = (configured or "").strip()
    if configured and Path(configured).is_file():
        return configured
    for candidate in (
        _bundle_root() / "config" / "rtsi" / filename,
        _app_dir() / "config" / "rtsi" / filename,
        _app_dir() / "rtsi" / filename,
    ):
        if candidate.is_file():
            return str(candidate)
    # 仍返回默认路径，便于日志定位缺失文件
    return configured or str(_bundle_root() / "config" / "rtsi" / filename)


def default_data_dir() -> Path:
    return Path.home() / ".robot_controller"


@dataclass
class ChassisCfg:
    host: str = "192.168.11.1"
    port: int = 1448
    timeout_s: float = 4.0


@dataclass
class ArmCfg:
    kind: str = "mock"
    host: str = "192.168.11.150"
    local_ip: str = ""
    max_joint_speed_deg_s: float = 15.0
    speed_limit_enabled: bool = True
    headless_mode: bool = True
    servoj_timeout_ms: int = 300
    servoj_time: float = 0.1
    rtsi_output_recipe: str = ""
    rtsi_input_recipe: str = ""
    skip_rtsi: bool = False


@dataclass
class CameraCfg:
    kind: str = "hikvision"
    host: str = "192.168.11.101"
    user: str = "admin"
    password: str = ""
    port: int = 554
    stream_path: str = "/h264/ch1/main/av_stream"
    rtsp_url: str = ""
    usb_index: int = 0


@dataclass
class RangingCfg:
    enabled: bool = False
    host: str = ""
    port: int = 0


@dataclass
class DevicesConfig:
    pc_local_ip: str = "192.168.11.10"
    chassis: ChassisCfg = field(default_factory=ChassisCfg)
    arm: ArmCfg = field(default_factory=ArmCfg)
    camera: CameraCfg = field(default_factory=CameraCfg)
    ranging: RangingCfg = field(default_factory=RangingCfg)
    data_dir: Path = field(default_factory=default_data_dir)

    def ensure_data_dir(self) -> Path:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "snapshots").mkdir(parents=True, exist_ok=True)
        return self.data_dir


def _deep_merge(base: dict, overlay: dict) -> dict:
    out = dict(base)
    for k, v in overlay.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _simple_yaml_load(text: str) -> dict[str, Any]:
    """极简 YAML 子集解析（仅支持本仓库 devices.yaml 缩进结构）。"""
    root: dict[str, Any] = {}
    stack: list[tuple[int, dict]] = [(-1, root)]
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        line = raw.strip()
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        while stack and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]
        if val == "":
            child: dict[str, Any] = {}
            parent[key] = child
            stack.append((indent, child))
        else:
            if val.lower() in ("true", "false"):
                parent[key] = val.lower() == "true"
            else:
                try:
                    if "." in val:
                        parent[key] = float(val)
                    else:
                        parent[key] = int(val)
                except ValueError:
                    parent[key] = val
    return root


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    text = path.read_text(encoding="utf-8")
    if yaml is not None:
        data = yaml.safe_load(text) or {}
    else:
        data = _simple_yaml_load(text)
    if not isinstance(data, dict):
        raise ValueError(f"config root must be mapping: {path}")
    return data


def load_devices_config(path: Optional[Path] = None) -> DevicesConfig:
    primary = path or default_config_path()
    raw = _load_yaml(primary)
    local = _find_local_overlay(primary)
    if local is not None:
        raw = _deep_merge(raw, _load_yaml(local))

    pc = raw.get("pc") or {}
    ch = raw.get("chassis") or {}
    ar = raw.get("arm") or {}
    ca = raw.get("camera") or {}
    rg = raw.get("ranging") or {}
    paths = raw.get("paths") or {}

    data_dir_s = (paths.get("data_dir") or "").strip()
    if data_dir_s:
        data_dir = Path(data_dir_s)
    elif getattr(sys, "frozen", False):
        # exe：业务数据落在可编辑配置包 config/data 下
        data_dir = _app_dir() / "config" / "data"
    else:
        data_dir = default_data_dir()

    cam_password = (ca.get("password") or "").strip()
    env_pw = (os.environ.get("ROBOT_CAMERA_PASSWORD") or "").strip()
    if env_pw:
        cam_password = env_pw

    local_ip = (pc.get("local_ip") or "").strip() or "192.168.11.10"

    rtsi_out = _resolve_rtsi_path(
        str(ar.get("rtsi_output_recipe") or ""), "output_recipe.txt"
    )
    rtsi_in = _resolve_rtsi_path(
        str(ar.get("rtsi_input_recipe") or ""), "input_recipe.txt"
    )

    cfg = DevicesConfig(
        pc_local_ip=local_ip,
        chassis=ChassisCfg(
            host=str(ch.get("host") or "192.168.11.1"),
            port=int(ch.get("port") or 1448),
            timeout_s=float(ch.get("timeout_s") or 4.0),
        ),
        arm=ArmCfg(
            kind=str(ar.get("kind") or "mock"),
            host=str(ar.get("host") or "192.168.11.150"),
            local_ip=local_ip,
            max_joint_speed_deg_s=float(ar.get("max_joint_speed_deg_s") or 15.0),
            speed_limit_enabled=bool(ar.get("speed_limit_enabled", True)),
            headless_mode=bool(ar.get("headless_mode", True)),
            servoj_timeout_ms=int(ar.get("servoj_timeout_ms") or 300),
            rtsi_output_recipe=rtsi_out,
            rtsi_input_recipe=rtsi_in,
            skip_rtsi=bool(ar.get("skip_rtsi", False)),
        ),
        camera=CameraCfg(
            kind=str(ca.get("kind") or "hikvision"),
            host=str(ca.get("host") or "192.168.11.101"),
            user=str(ca.get("user") or "admin"),
            password=cam_password,
            port=int(ca.get("port") or 554),
            stream_path=str(ca.get("stream_path") or "/h264/ch1/main/av_stream"),
            rtsp_url=str(ca.get("rtsp_url") or ""),
            usb_index=int(ca.get("usb_index") or 0),
        ),
        ranging=RangingCfg(
            enabled=bool(rg.get("enabled", False)),
            host=str(rg.get("host") or ""),
            port=int(rg.get("port") or 0),
        ),
        data_dir=data_dir,
    )
    return cfg
