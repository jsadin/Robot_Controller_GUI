"""机械臂控制门面（对齐原 TeleopController，无 Qt）。"""

from __future__ import annotations

import time
from typing import Optional, Protocol, Sequence

from devices.arm.joint_servo_planner import JointServoTrajectoryPlanner
from devices.arm.mock_backend import MockArmBackend
from devices.arm.types import JointState6
from devices.config_loader import ArmCfg, DevicesConfig


class _ArmBackend(Protocol):
    def connect(self) -> bool: ...
    def close(self) -> None: ...
    def is_connected(self) -> bool: ...
    def emergency_stop(self) -> None: ...
    def clear_emergency_stop(self) -> None: ...
    def read_joints_rad(self) -> JointState6 | None: ...
    def command_joints_rad(self, joints: JointState6, timeout_ms: int | None = None) -> bool: ...
    def brake_release(self) -> bool: ...
    def start_freedrive(self) -> bool: ...
    def stop_freedrive(self) -> bool: ...


def build_arm_backend(cfg: DevicesConfig) -> _ArmBackend:
    kind = (cfg.arm.kind or "mock").strip().lower()
    if kind == "mock":
        return MockArmBackend()
    if kind in ("elite_cs", "elite", "elite_cs_sdk"):
        from devices.arm.elite_config import EliteBackendConfig
        from devices.arm.elite_robot import EliteCsRobotBackend

        ec = EliteBackendConfig(
            robot_ip=cfg.arm.host,
            local_ip=cfg.arm.local_ip or cfg.pc_local_ip,
            headless_mode=cfg.arm.headless_mode,
            servoj_timeout_ms=cfg.arm.servoj_timeout_ms,
            servoj_time=cfg.arm.servoj_time,
            skip_rtsi=bool(cfg.arm.skip_rtsi),
            rtsi_output_recipe=str(cfg.arm.rtsi_output_recipe or ""),
            rtsi_input_recipe=str(cfg.arm.rtsi_input_recipe or ""),
        )
        return EliteCsRobotBackend(ec)
    raise ValueError(f"unknown arm kind: {cfg.arm.kind!r}")


class ArmController:
    """UI / Mission 调用的稳定入口。"""

    def __init__(self, cfg: DevicesConfig) -> None:
        self.config = cfg
        self._arm_cfg: ArmCfg = cfg.arm
        self._robot: _ArmBackend = build_arm_backend(cfg)
        self._motion_halted = False
        self._joint_planner = JointServoTrajectoryPlanner(
            max_joint_speed_deg_s=float(cfg.arm.max_joint_speed_deg_s),
            speed_limit_enabled=bool(cfg.arm.speed_limit_enabled),
            servoj_time_s=float(cfg.arm.servoj_time),
            default_tick_s=1.0 / 50.0,
        )

    @property
    def motion_halted(self) -> bool:
        return self._motion_halted

    def is_connected(self) -> bool:
        return self._robot.is_connected()

    def last_connect_error(self) -> str:
        return getattr(self._robot, "last_connect_error", "") or ""

    def connect(self) -> bool:
        ok = self._robot.connect()
        if ok:
            self._motion_halted = False
            self._robot.clear_emergency_stop()
            self.seed_from_feedback()
        return ok

    def disconnect(self) -> None:
        self._robot.close()

    def emergency_stop(self) -> None:
        self._motion_halted = True
        self._robot.emergency_stop()

    def clear_emergency_stop(self) -> None:
        """解除软件闩锁，并让后端退出 idle / 重同步规划器。"""
        self._motion_halted = False
        self._robot.clear_emergency_stop()
        # 以当前反馈为期望角，避免解除后按急停前目标猛冲或卡死
        try:
            self.seed_from_feedback()
        except Exception:
            pass

    def seed_from_feedback(self) -> None:
        j = self._robot.read_joints_rad()
        if j is None:
            baseline = getattr(self._robot, "command_baseline_rad", None)
            if callable(baseline):
                j = baseline()
        if j is not None:
            self._joint_planner.reset(cmd=j, desired=j)

    def sync_joint_desired_deg(self, deg6: Sequence[float]) -> None:
        self._joint_planner.set_desired(JointState6.from_degrees(deg6))

    def advance_joint_command(self, *, timeout_ms: Optional[int] = None) -> bool:
        if self._motion_halted or not self._robot.is_connected():
            return False
        a = self._arm_cfg
        self._joint_planner.sync_runtime_config(
            max_joint_speed_deg_s=float(a.max_joint_speed_deg_s),
            speed_limit_enabled=bool(a.speed_limit_enabled),
            servoj_time_s=float(a.servoj_time),
            dt_wall_cap_s=0.05,
            servo_step_margin=0.92,
        )
        sample = self._joint_planner.step_toward_desired()
        return self._robot.command_joints_rad(sample, timeout_ms)

    def flush_joint_steps(self, steps: int = 8) -> bool:
        """离散拖动滑块时连发几步，无需勾选持续流控。"""
        ok = True
        for _ in range(max(1, int(steps))):
            ok = self.advance_joint_command(timeout_ms=None) and ok
        return ok

    def read_joints_deg(self) -> tuple[float, ...] | None:
        j = self._robot.read_joints_rad()
        if j is None:
            # RTSI 暂不可用时回退到上次指令/连接种子，避免 UI 一直停在 0°
            last = getattr(self._robot, "last_commanded_deg6", None)
            if callable(last):
                return last()
            return None
        return j.as_degrees()

    def set_joints_deg_blocking(self, deg6: Sequence[float], timeout_s: float = 120.0) -> bool:
        self.sync_joint_desired_deg(deg6)
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self._joint_planner.at_target():
                return True
            self.advance_joint_command(timeout_ms=None)
            time.sleep(0.02)
        return self._joint_planner.at_target()

    def joint_at_target(self) -> bool:
        return self._joint_planner.at_target()

    # ---- 预留（功能表明细 stub）----
    def brake_release(self) -> bool:
        return bool(self._robot.brake_release())

    def start_freedrive(self) -> bool:
        return bool(self._robot.start_freedrive())

    def stop_freedrive(self) -> bool:
        return bool(self._robot.stop_freedrive())
