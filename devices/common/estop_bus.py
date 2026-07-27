"""系统级急停总线：一点急停联动底盘 + 机械臂 + 停任务。"""

from __future__ import annotations

from typing import Callable, List, Optional, Protocol


class _EStopChassis(Protocol):
    def set_emergency_stop(self, on: bool) -> None: ...


class _EStopArm(Protocol):
    def emergency_stop(self) -> None: ...

    def clear_emergency_stop(self) -> None: ...


class EStopBus:
    """全局急停编排（无 Qt）。"""

    def __init__(self) -> None:
        self._latched = False
        self._chassis: Optional[_EStopChassis] = None
        self._arm: Optional[_EStopArm] = None
        self._stop_mission: Optional[Callable[[], None]] = None
        self._listeners: List[Callable[[bool], None]] = []

    def bind(
        self,
        *,
        chassis: Optional[_EStopChassis] = None,
        arm: Optional[_EStopArm] = None,
        stop_mission: Optional[Callable[[], None]] = None,
    ) -> None:
        if chassis is not None:
            self._chassis = chassis
        if arm is not None:
            self._arm = arm
        if stop_mission is not None:
            self._stop_mission = stop_mission

    def add_listener(self, cb: Callable[[bool], None]) -> None:
        self._listeners.append(cb)

    @property
    def latched(self) -> bool:
        return self._latched

    def trigger(self) -> None:
        self._latched = True
        if self._stop_mission:
            try:
                self._stop_mission()
            except Exception:
                pass
        if self._chassis is not None:
            try:
                self._chassis.set_emergency_stop(True)
            except Exception:
                pass
        if self._arm is not None:
            try:
                self._arm.emergency_stop()
            except Exception:
                pass
        for cb in list(self._listeners):
            try:
                cb(True)
            except Exception:
                pass

    def release(self) -> None:
        self._latched = False
        if self._chassis is not None:
            try:
                self._chassis.set_emergency_stop(False)
            except Exception:
                pass
        if self._arm is not None:
            try:
                self._arm.clear_emergency_stop()
            except Exception:
                pass
        for cb in list(self._listeners):
            try:
                cb(False)
            except Exception:
                pass
