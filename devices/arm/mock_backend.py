"""Mock 机械臂后端（CI / 无硬件）。"""

from __future__ import annotations

import math

from devices.arm.types import CartesianTarget, JointState6


class MockArmBackend:
    def __init__(self) -> None:
        self._connected = False
        self._q = JointState6((0.0, 0.0, 0.0, 0.0, 0.0, 0.0))
        self._estop_latch = False
        self.last_connect_error = ""

    def connect(self) -> bool:
        self._connected = True
        self._estop_latch = False
        self.last_connect_error = ""
        return True

    def close(self) -> None:
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected

    def emergency_stop(self) -> None:
        self._estop_latch = True

    def clear_emergency_stop(self) -> None:
        self._estop_latch = False

    def read_joints_rad(self) -> JointState6 | None:
        if not self._connected:
            return None
        return self._q

    def command_baseline_rad(self) -> JointState6:
        return self._q

    def last_commanded_deg6(self) -> tuple[float, float, float, float, float, float]:
        return self._q.as_degrees()

    def command_joints_rad(self, joints: JointState6, timeout_ms: int | None = None) -> bool:
        if not self._connected or self._estop_latch:
            return False
        self._q = joints
        _ = timeout_ms
        return True

    def command_cartesian(self, pose: CartesianTarget, timeout_ms: int | None = None) -> bool:
        if not self._connected or self._estop_latch:
            return False
        self._q = JointState6(
            (
                math.radians(pose.z * 10.0),
                self._q.q[1],
                self._q.q[2],
                self._q.q[3],
                self._q.q[4],
                self._q.q[5],
            )
        )
        _ = timeout_ms
        return True

    def brake_release(self) -> bool:
        return False

    def start_freedrive(self) -> bool:
        return False

    def stop_freedrive(self) -> bool:
        return False
