"""统一设备状态与告警类型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List


class DeviceId(str, Enum):
    CHASSIS = "chassis"
    ARM = "arm"
    CAMERA = "camera"
    RANGING = "ranging"


class ConnectionState(str, Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    ERROR = "error"


class AlarmLevel(str, Enum):
    INFO = "info"
    WARN = "warn"
    ERROR = "error"
    CRITICAL = "critical"


class OverallHealth(str, Enum):
    OK = "ok"
    DEGRADED = "degraded"
    FAULT = "fault"


@dataclass
class AlarmItem:
    device: DeviceId
    code: str
    message: str
    level: AlarmLevel = AlarmLevel.WARN


@dataclass
class DeviceStatus:
    device: DeviceId
    state: ConnectionState = ConnectionState.DISCONNECTED
    detail: str = ""
    alarms: List[AlarmItem] = field(default_factory=list)
    metrics: Dict[str, Any] = field(default_factory=dict)
    ok: bool = True


@dataclass
class HealthSummary:
    overall: OverallHealth = OverallHealth.OK
    fault_count: int = 0
    warn_count: int = 0
    generated_at: float = 0.0
