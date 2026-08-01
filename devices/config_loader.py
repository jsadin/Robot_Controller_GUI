"""加载 config/devices.yaml（及可选 devices.local.yaml）。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def default_config_path() -> Path:
    env = (os.environ.get("ROBOT_CONFIG") or "").strip()
    if env:
        return Path(env)
    return _repo_root() / "config" / "devices.yaml"


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
    max_joint_speed_deg_s: float = 45.0
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
    local = primary.with_name("devices.local.yaml")
    if local.is_file():
        raw = _deep_merge(raw, _load_yaml(local))

    pc = raw.get("pc") or {}
    ch = raw.get("chassis") or {}
    ar = raw.get("arm") or {}
    ca = raw.get("camera") or {}
    rg = raw.get("ranging") or {}
    paths = raw.get("paths") or {}

    data_dir_s = (paths.get("data_dir") or "").strip()
    data_dir = Path(data_dir_s) if data_dir_s else default_data_dir()

    cam_password = (ca.get("password") or "").strip()
    env_pw = (os.environ.get("ROBOT_CAMERA_PASSWORD") or "").strip()
    if env_pw:
        cam_password = env_pw

    local_ip = (pc.get("local_ip") or "").strip() or "192.168.11.10"

    default_rtsi_out = str(_repo_root() / "config" / "rtsi" / "output_recipe.txt")
    default_rtsi_in = str(_repo_root() / "config" / "rtsi" / "input_recipe.txt")
    rtsi_out = str(ar.get("rtsi_output_recipe") or "").strip() or default_rtsi_out
    rtsi_in = str(ar.get("rtsi_input_recipe") or "").strip() or default_rtsi_in

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
            max_joint_speed_deg_s=float(ar.get("max_joint_speed_deg_s") or 45.0),
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
