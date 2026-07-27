"""Rate-limited joint interpolation toward a moving target (discrete servoj)."""

from __future__ import annotations

import math
import time

from devices.arm.types import JointState6


class JointServoTrajectoryPlanner:
    """
    Elite ``external_control`` applies each servoj setpoint over roughly ``servoj_time``.
    If the commanded delta per packet exceeds what is achievable in that window, the
    controller rejects the command (velocity limit). This planner advances an internal
    command ``q_cmd`` toward ``q_des`` so each step respects:

    - ``|Δq_i| ≤ ω_max · min(Δt_wall, dt_cap)``
    - ``|Δq_i| ≤ ω_max · servoj_time · margin`` (per-packet servoj window)

    where ``ω_max`` comes from the UI joint speed limit (rad/s).
    """

    def __init__(
        self,
        *,
        max_joint_speed_deg_s: float,
        speed_limit_enabled: bool,
        servoj_time_s: float,
        dt_wall_cap_s: float = 0.05,
        servo_step_margin: float = 0.92,
        epsilon_rad: float = 5e-5,
        default_tick_s: float = 0.05,
    ) -> None:
        self._max_deg_s = float(max_joint_speed_deg_s)
        self._speed_limit_enabled = bool(speed_limit_enabled)
        self._servoj_time_s = max(float(servoj_time_s), 1e-6)
        self._dt_wall_cap = max(float(dt_wall_cap_s), 1e-4)
        self._servo_margin = max(min(float(servo_step_margin), 1.0), 0.05)
        self._epsilon = float(epsilon_rad)
        self._default_tick_s = max(float(default_tick_s), 1e-4)

        self._desired = JointState6((0.0, 0.0, 0.0, 0.0, 0.0, 0.0))
        self._cmd = JointState6((0.0, 0.0, 0.0, 0.0, 0.0, 0.0))
        self._last_tick_mono: float | None = None

    def sync_runtime_config(
        self,
        *,
        max_joint_speed_deg_s: float,
        speed_limit_enabled: bool,
        servoj_time_s: float,
        dt_wall_cap_s: float,
        servo_step_margin: float,
    ) -> None:
        """Keep planner bounds aligned with UI / RobotConfig (call once per command tick)."""

        self._max_deg_s = float(max_joint_speed_deg_s)
        self._speed_limit_enabled = bool(speed_limit_enabled)
        self._servoj_time_s = max(float(servoj_time_s), 1e-6)
        self._dt_wall_cap = max(float(dt_wall_cap_s), 1e-4)
        self._servo_margin = max(min(float(servo_step_margin), 1.0), 0.05)

    def reset(self, cmd: JointState6, desired: JointState6 | None = None) -> None:
        """Snap commanded pose (and optionally desired) — call after connect / feedback sync."""

        self._cmd = cmd
        self._desired = desired if desired is not None else cmd
        self._last_tick_mono = None

    def set_desired(self, desired: JointState6) -> None:
        self._desired = desired

    @property
    def commanded(self) -> JointState6:
        return self._cmd

    @property
    def desired(self) -> JointState6:
        return self._desired

    def at_target(self) -> bool:
        return self._distance_rad(self._cmd, self._desired) <= self._epsilon

    def step_toward_desired(self) -> JointState6:
        """Advance ``_cmd`` one tick toward ``_desired``; return the new command."""

        now = time.monotonic()
        if self._last_tick_mono is None:
            dt = self._default_tick_s
        else:
            dt = now - self._last_tick_mono
        dt = max(dt, 1e-4)
        dt = min(dt, self._dt_wall_cap)
        self._last_tick_mono = now

        if not self._speed_limit_enabled or self._max_deg_s <= 0:
            self._cmd = self._desired
            return self._cmd

        ω = math.radians(self._max_deg_s)
        step_wall = ω * dt
        step_servo = ω * self._servoj_time_s * self._servo_margin
        max_step = min(step_wall, step_servo)

        new_q = list(self._cmd.q)
        for i in range(6):
            d = self._desired.q[i] - new_q[i]
            if abs(d) <= max_step:
                new_q[i] = self._desired.q[i]
            else:
                new_q[i] += math.copysign(max_step, d)
        self._cmd = JointState6(tuple(new_q))
        return self._cmd

    @staticmethod
    def _distance_rad(a: JointState6, b: JointState6) -> float:
        return max(abs(a.q[i] - b.q[i]) for i in range(6))
