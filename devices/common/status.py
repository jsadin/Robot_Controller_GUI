"""统一设备状态与告警类型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List


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
