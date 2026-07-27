"""底盘 Hermes REST 客户端（迁自 SLAM/hermes）。"""

from devices.chassis.client import (
    DIR_BACKWARD,
    DIR_FORWARD,
    DIR_TURN_LEFT,
    DIR_TURN_RIGHT,
    HermesClient,
    HermesError,
    LaserScan,
    POI,
    Pose,
    PowerStatus,
)

# 对外别名：规范名 ChassisClient
ChassisClient = HermesClient

__all__ = [
    "ChassisClient",
    "HermesClient",
    "HermesError",
    "Pose",
    "PowerStatus",
    "POI",
    "LaserScan",
    "DIR_FORWARD",
    "DIR_BACKWARD",
    "DIR_TURN_LEFT",
    "DIR_TURN_RIGHT",
]
