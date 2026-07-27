"""诊断聚合：汇总各设备状态供诊断面板消费。"""

from __future__ import annotations

from typing import List, Optional

from devices.arm.controller import ArmController
from devices.camera import CameraBackend
from devices.chassis import ChassisClient, HermesError
from devices.common.status import (
    AlarmItem,
    AlarmLevel,
    ConnectionState,
    DeviceId,
    DeviceStatus,
)
from devices.ranging import RangingBackend


class DiagnosisAggregator:
    def __init__(
        self,
        *,
        chassis: Optional[ChassisClient] = None,
        arm: Optional[ArmController] = None,
        camera: Optional[CameraBackend] = None,
        ranging: Optional[RangingBackend] = None,
    ) -> None:
        self.chassis = chassis
        self.arm = arm
        self.camera = camera
        self.ranging = ranging

    def collect(self) -> List[DeviceStatus]:
        return [
            self._chassis_status(),
            self._arm_status(),
            self._camera_status(),
            self._ranging_status(),
        ]

    def _chassis_status(self) -> DeviceStatus:
        if self.chassis is None:
            return DeviceStatus(DeviceId.CHASSIS, ConnectionState.DISCONNECTED, "未绑定")
        try:
            info = self.chassis.get_health_items()
            alarms = []
            if info.get("emergency_stop"):
                alarms.append(
                    AlarmItem(DeviceId.CHASSIS, "ESTOP", "底盘急停中", AlarmLevel.CRITICAL)
                )
            if info.get("lidar_disconnected"):
                alarms.append(
                    AlarmItem(DeviceId.CHASSIS, "LIDAR", "雷达断开", AlarmLevel.WARN)
                )
            for e in info.get("errors") or []:
                alarms.append(
                    AlarmItem(
                        DeviceId.CHASSIS,
                        str(e.get("code") or "ERR"),
                        str(e.get("message") or ""),
                        AlarmLevel.ERROR,
                    )
                )
            return DeviceStatus(
                DeviceId.CHASSIS,
                ConnectionState.CONNECTED,
                f"health ok, alarms={len(alarms)}",
                alarms,
            )
        except HermesError as e:
            return DeviceStatus(
                DeviceId.CHASSIS,
                ConnectionState.ERROR,
                str(e),
                [AlarmItem(DeviceId.CHASSIS, "COMM", str(e), AlarmLevel.ERROR)],
            )
        except Exception as e:
            return DeviceStatus(DeviceId.CHASSIS, ConnectionState.ERROR, str(e))

    def _arm_status(self) -> DeviceStatus:
        if self.arm is None:
            return DeviceStatus(DeviceId.ARM, ConnectionState.DISCONNECTED, "未绑定")
        if not self.arm.is_connected():
            err = self.arm.last_connect_error()
            return DeviceStatus(
                DeviceId.ARM,
                ConnectionState.DISCONNECTED,
                err or "未连接",
            )
        alarms = []
        if self.arm.motion_halted:
            alarms.append(
                AlarmItem(DeviceId.ARM, "ESTOP", "臂软件急停闩锁", AlarmLevel.CRITICAL)
            )
        return DeviceStatus(DeviceId.ARM, ConnectionState.CONNECTED, "已连接", alarms)

    def _camera_status(self) -> DeviceStatus:
        if self.camera is None:
            return DeviceStatus(DeviceId.CAMERA, ConnectionState.DISCONNECTED, "未绑定")
        frame = None
        try:
            frame = self.camera.read_bgr()
        except Exception as e:
            return DeviceStatus(DeviceId.CAMERA, ConnectionState.ERROR, str(e))
        if frame is None:
            return DeviceStatus(DeviceId.CAMERA, ConnectionState.DISCONNECTED, "无画面")
        h, w = frame.shape[:2]
        return DeviceStatus(DeviceId.CAMERA, ConnectionState.CONNECTED, f"{w}x{h}")

    def _ranging_status(self) -> DeviceStatus:
        if self.ranging is None or not self.ranging.enabled:
            return DeviceStatus(DeviceId.RANGING, ConnectionState.DISCONNECTED, "未启用(stub)")
        d = self.ranging.get_distance_m()
        if d is None:
            return DeviceStatus(DeviceId.RANGING, ConnectionState.DISCONNECTED, "无数据")
        return DeviceStatus(DeviceId.RANGING, ConnectionState.CONNECTED, f"{d:.3f} m")
