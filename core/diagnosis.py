"""诊断聚合：汇总各设备运行态与告警，供诊断面板与日志导出消费。"""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from devices.arm.controller import ArmController
from devices.camera import CameraBackend
from devices.chassis import ChassisClient, HermesError
from devices.common.status import (
    AlarmItem,
    AlarmLevel,
    ConnectionState,
    DeviceId,
    DeviceStatus,
    HealthSummary,
    OverallHealth,
)
from devices.ranging import RangingBackend

# 定位质量低于此值记 WARN（与主窗口提示阈值一致）
LOC_QUALITY_WARN = 50
# 相机无新帧超时（秒）
CAMERA_STALE_SECS = 5.0


def _hermes_level(level: Any) -> AlarmLevel:
    try:
        lv = int(level)
    except (TypeError, ValueError):
        return AlarmLevel.ERROR
    if lv >= 3:
        return AlarmLevel.CRITICAL
    if lv >= 2:
        return AlarmLevel.ERROR
    if lv == 1:
        return AlarmLevel.WARN
    return AlarmLevel.INFO


def _alarm_rank(level: AlarmLevel) -> int:
    return {
        AlarmLevel.INFO: 0,
        AlarmLevel.WARN: 1,
        AlarmLevel.ERROR: 2,
        AlarmLevel.CRITICAL: 3,
    }.get(level, 0)


class DiagnosisAggregator:
    def __init__(
        self,
        *,
        chassis: Optional[ChassisClient] = None,
        arm: Optional[ArmController] = None,
        camera: Optional[CameraBackend] = None,
        ranging: Optional[RangingBackend] = None,
        get_arm_streaming: Optional[Callable[[], bool]] = None,
    ) -> None:
        self.chassis = chassis
        self.arm = arm
        self.camera = camera
        self.ranging = ranging
        self.get_arm_streaming = get_arm_streaming
        # 静默连接失败时供诊断展示（chassis 仍为 None）
        self.chassis_last_error: Optional[str] = None
        self.boot_notes: List[str] = []

    def collect(self) -> List[DeviceStatus]:
        return self.collect_with_summary()[0]

    def collect_with_summary(self) -> Tuple[List[DeviceStatus], HealthSummary]:
        statuses = [
            self._chassis_status(),
            self._arm_status(),
            self._camera_status(),
            self._ranging_status(),
        ]
        return statuses, self._summarize(statuses)

    def collect_snapshot(self) -> Dict[str, Any]:
        """JSON 可序列化诊断快照（供日志导出）。"""
        statuses, summary = self.collect_with_summary()
        return {
            "generated_at": summary.generated_at,
            "summary": {
                "overall": summary.overall.value,
                "fault_count": summary.fault_count,
                "warn_count": summary.warn_count,
            },
            "boot_notes": list(self.boot_notes or []),
            "devices": [self._status_to_dict(s) for s in statuses],
        }

    @staticmethod
    def _status_to_dict(s: DeviceStatus) -> Dict[str, Any]:
        return {
            "device": s.device.value,
            "state": s.state.value,
            "detail": s.detail,
            "ok": s.ok,
            "metrics": dict(s.metrics or {}),
            "alarms": [
                {
                    "device": a.device.value,
                    "code": a.code,
                    "message": a.message,
                    "level": a.level.value,
                }
                for a in (s.alarms or [])
            ],
        }

    def _summarize(self, statuses: List[DeviceStatus]) -> HealthSummary:
        fault = 0
        warn = 0
        for s in statuses:
            for a in s.alarms or []:
                if _alarm_rank(a.level) >= _alarm_rank(AlarmLevel.ERROR):
                    fault += 1
                elif a.level == AlarmLevel.WARN:
                    warn += 1
            if s.state == ConnectionState.ERROR or not s.ok:
                if not (s.alarms or []):
                    # 无显式告警但设备异常 → 计故障或降级
                    if s.state == ConnectionState.ERROR:
                        fault += 1
                    else:
                        warn += 1
        if fault > 0:
            overall = OverallHealth.FAULT
        elif warn > 0:
            overall = OverallHealth.DEGRADED
        else:
            overall = OverallHealth.OK
        return HealthSummary(
            overall=overall,
            fault_count=fault,
            warn_count=warn,
            generated_at=time.time(),
        )

    def _chassis_status(self) -> DeviceStatus:
        if self.chassis is None:
            if self.chassis_last_error:
                return DeviceStatus(
                    DeviceId.CHASSIS,
                    ConnectionState.ERROR,
                    self.chassis_last_error,
                    [
                        AlarmItem(
                            DeviceId.CHASSIS,
                            "BOOT",
                            self.chassis_last_error,
                            AlarmLevel.ERROR,
                        )
                    ],
                    ok=False,
                )
            return DeviceStatus(
                DeviceId.CHASSIS, ConnectionState.DISCONNECTED, "未连接", ok=True
            )
        try:
            info = self.chassis.get_health_items()
            alarms: List[AlarmItem] = []
            metrics: Dict[str, Any] = {
                "emergency_stop": bool(info.get("emergency_stop")),
                "lidar_disconnected": bool(info.get("lidar_disconnected")),
            }

            if info.get("emergency_stop"):
                alarms.append(
                    AlarmItem(
                        DeviceId.CHASSIS, "ESTOP", "底盘急停中", AlarmLevel.CRITICAL
                    )
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
                        _hermes_level(e.get("level")),
                    )
                )

            # 电量 / 定位 / 当前动作（失败不阻断主诊断）
            try:
                p = self.chassis.get_power_status()
                metrics["battery_pct"] = getattr(p, "battery_percentage", None)
                metrics["charging"] = bool(getattr(p, "is_charging", False))
            except Exception:
                pass
            try:
                q = self.chassis.get_localization_quality()
                if isinstance(q, int):
                    metrics["loc_quality"] = q
                    if q < LOC_QUALITY_WARN:
                        alarms.append(
                            AlarmItem(
                                DeviceId.CHASSIS,
                                "LOC_QUALITY",
                                f"定位质量低({q})",
                                AlarmLevel.WARN,
                            )
                        )
            except Exception:
                pass
            try:
                act = self.chassis.get_current_action()
                if act:
                    stage = ""
                    if isinstance(act, dict):
                        st = act.get("state") or {}
                        stage = st.get("status") if isinstance(st, dict) else ""
                        name = act.get("action_name") or act.get("name") or "action"
                    else:
                        name = "action"
                    metrics["action"] = f"{name}{(' ' + str(stage)) if stage else ''}"
            except Exception:
                pass

            max_rank = max((_alarm_rank(a.level) for a in alarms), default=0)
            if max_rank >= _alarm_rank(AlarmLevel.ERROR):
                state = ConnectionState.ERROR
                ok = False
            else:
                state = ConnectionState.CONNECTED
                ok = max_rank < _alarm_rank(AlarmLevel.WARN)

            parts = []
            bat = metrics.get("battery_pct")
            if bat is not None:
                ch = "充电" if metrics.get("charging") else "未充"
                parts.append(f"电量{bat}%({ch})")
            lq = metrics.get("loc_quality")
            if lq is not None:
                parts.append(f"定位q={lq}")
            if metrics.get("action"):
                parts.append(str(metrics["action"]))
            if max_rank >= _alarm_rank(AlarmLevel.ERROR):
                n_bad = sum(
                    1
                    for a in alarms
                    if _alarm_rank(a.level) >= _alarm_rank(AlarmLevel.ERROR)
                )
                parts.append(f"健康异常{n_bad}条")
            elif max_rank == _alarm_rank(AlarmLevel.WARN):
                parts.append(f"警告{len(alarms)}条")
            detail = " | ".join(parts) if parts else "运行正常"

            return DeviceStatus(
                DeviceId.CHASSIS, state, detail, alarms, metrics=metrics, ok=ok
            )
        except HermesError as e:
            return DeviceStatus(
                DeviceId.CHASSIS,
                ConnectionState.ERROR,
                str(e),
                [AlarmItem(DeviceId.CHASSIS, "COMM", str(e), AlarmLevel.ERROR)],
                ok=False,
            )
        except Exception as e:
            return DeviceStatus(
                DeviceId.CHASSIS, ConnectionState.ERROR, str(e), ok=False
            )

    def _arm_status(self) -> DeviceStatus:
        if self.arm is None:
            return DeviceStatus(
                DeviceId.ARM, ConnectionState.DISCONNECTED, "未绑定", ok=True
            )
        if not self.arm.is_connected():
            err = self.arm.last_connect_error()
            return DeviceStatus(
                DeviceId.ARM,
                ConnectionState.DISCONNECTED if not err else ConnectionState.ERROR,
                err or "未连接",
                (
                    [
                        AlarmItem(
                            DeviceId.ARM, "CONNECT", err, AlarmLevel.ERROR
                        )
                    ]
                    if err
                    else []
                ),
                ok=not bool(err),
            )
        alarms: List[AlarmItem] = []
        streaming = False
        if callable(self.get_arm_streaming):
            try:
                streaming = bool(self.get_arm_streaming())
            except Exception:
                streaming = False
        metrics = {
            "streaming": streaming,
            "motion_halted": bool(self.arm.motion_halted),
        }
        # 关节可读性
        try:
            joints = self.arm.read_joints_deg()
            metrics["joints_readable"] = joints is not None
        except Exception:
            metrics["joints_readable"] = False
        if self.arm.motion_halted:
            alarms.append(
                AlarmItem(
                    DeviceId.ARM, "ESTOP", "臂软件急停闩锁", AlarmLevel.CRITICAL
                )
            )
        stream_s = "流控开" if streaming else "流控关"
        halt_s = "急停闩锁" if self.arm.motion_halted else "可运动"
        detail = f"已连接 | {stream_s} | {halt_s}"
        ok = not bool(alarms)
        state = ConnectionState.ERROR if alarms else ConnectionState.CONNECTED
        return DeviceStatus(
            DeviceId.ARM, state, detail, alarms, metrics=metrics, ok=ok
        )

    def _camera_status(self) -> DeviceStatus:
        if self.camera is None:
            return DeviceStatus(
                DeviceId.CAMERA, ConnectionState.DISCONNECTED, "未绑定", ok=True
            )
        opened = False
        try:
            opened = bool(self.camera.is_open())
        except Exception:
            opened = False

        # 先读帧龄再 read：OpenCV 读缓存不刷新时间戳，可据此判断流
        age_before = self._camera_frame_age_s()
        frame = None
        try:
            frame = self.camera.read_bgr()
        except Exception as e:
            return DeviceStatus(
                DeviceId.CAMERA,
                ConnectionState.ERROR,
                str(e),
                [AlarmItem(DeviceId.CAMERA, "READ", str(e), AlarmLevel.ERROR)],
                ok=False,
            )
        age = self._camera_frame_age_s()
        # 若 read 会刷新时间戳（如 Mock），断流判定用读前帧龄
        stale_age = age_before if age_before is not None else age

        metrics: Dict[str, Any] = {}
        if stale_age is not None:
            metrics["frame_age_s"] = round(float(stale_age), 2)

        if frame is not None:
            h, w = frame.shape[:2]
            metrics["width"] = w
            metrics["height"] = h
            if stale_age is not None and stale_age > CAMERA_STALE_SECS:
                return DeviceStatus(
                    DeviceId.CAMERA,
                    ConnectionState.ERROR,
                    f"{w}x{h} | 断流 {stale_age:.1f}s",
                    [
                        AlarmItem(
                            DeviceId.CAMERA,
                            "STALE_FRAME",
                            f"超过 {CAMERA_STALE_SECS:.0f}s 无新帧",
                            AlarmLevel.ERROR,
                        )
                    ],
                    metrics=metrics,
                    ok=False,
                )
            return DeviceStatus(
                DeviceId.CAMERA,
                ConnectionState.CONNECTED,
                f"{w}x{h}",
                metrics=metrics,
                ok=True,
            )

        if opened:
            open_age = None
            try:
                ots = getattr(self.camera, "opened_at", None)
                if ots is not None:
                    open_age = max(0.0, time.monotonic() - float(ots))
            except Exception:
                open_age = None
            wait = open_age if open_age is not None else stale_age
            if wait is not None and wait >= CAMERA_STALE_SECS:
                return DeviceStatus(
                    DeviceId.CAMERA,
                    ConnectionState.ERROR,
                    f"无帧超时 {wait:.1f}s",
                    [
                        AlarmItem(
                            DeviceId.CAMERA,
                            "STALE_FRAME",
                            f"已打开但超过 {CAMERA_STALE_SECS:.0f}s 无帧",
                            AlarmLevel.ERROR,
                        )
                    ],
                    metrics=metrics,
                    ok=False,
                )
            return DeviceStatus(
                DeviceId.CAMERA,
                ConnectionState.CONNECTING,
                "已打开，等待首帧",
                metrics=metrics,
                ok=True,
            )
        return DeviceStatus(
            DeviceId.CAMERA, ConnectionState.DISCONNECTED, "未打开", ok=True
        )

    def _camera_frame_age_s(self) -> Optional[float]:
        if self.camera is None:
            return None
        try:
            getter = getattr(self.camera, "frame_age_s", None)
            if callable(getter):
                return getter()
            ts = getattr(self.camera, "last_frame_ts", None)
            if ts is not None:
                return max(0.0, time.monotonic() - float(ts))
        except Exception:
            return None
        return None

    def _ranging_status(self) -> DeviceStatus:
        if self.ranging is None or not self.ranging.enabled:
            return DeviceStatus(
                DeviceId.RANGING,
                ConnectionState.DISCONNECTED,
                "未启用(stub)",
                ok=True,
            )
        d = self.ranging.get_distance_m()
        if d is None:
            return DeviceStatus(
                DeviceId.RANGING, ConnectionState.DISCONNECTED, "无数据", ok=True
            )
        return DeviceStatus(
            DeviceId.RANGING,
            ConnectionState.CONNECTED,
            f"{d:.3f} m",
            metrics={"distance_m": d},
            ok=True,
        )
