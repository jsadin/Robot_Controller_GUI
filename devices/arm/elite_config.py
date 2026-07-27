"""Elite CS 后端配置（无 Qt）。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class EliteBackendConfig:
    robot_ip: str = "192.168.11.150"
    local_ip: str = ""
    script_file_path: str = ""
    headless_mode: bool = True
    servoj_time: float = 0.1
    servoj_gain: int = 2000
    servoj_lookahead_time: float = 0.3
    connect_timeout_s: float = 30.0
    reconnect_settle_s: float = 2.0
    connect_retries: int = 3
    skip_rtsi: bool = False
    rtsi_snapshot_on_connect: bool = True
    bootstrap_joints_deg: tuple[float, ...] | None = None
    servoj_timeout_ms: int = 300
    servoj_hold_timeout_ms: int = 0
    manual_hold_keepalive_s: float = 1.0
    idle_command_timeout_ms: int = 100
    rtsi_output_recipe: str = ""
    rtsi_input_recipe: str = ""
    rtsi_frequency_hz: float = 250.0
