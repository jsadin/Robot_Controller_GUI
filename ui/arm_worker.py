"""机械臂控制循环线程（~50Hz），避免阻塞 PyQt 主线程。"""

from __future__ import annotations

import threading
import time
from typing import Callable, Optional, Sequence

from devices.arm.controller import ArmController


class ArmControlWorker:
    def __init__(self, arm: ArmController) -> None:
        self.arm = arm
        self._desired: Optional[tuple[float, ...]] = None
        self._streaming = False
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self.on_joints: Optional[Callable[[tuple[float, ...]], None]] = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="arm_control", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None

    def set_streaming(self, on: bool) -> None:
        self._streaming = bool(on)

    def set_desired_deg(self, deg6: Sequence[float]) -> None:
        with self._lock:
            self._desired = tuple(float(x) for x in deg6)

    def _loop(self) -> None:
        while not self._stop.is_set():
            t0 = time.monotonic()
            try:
                with self._lock:
                    desired = self._desired
                if desired is not None:
                    self.arm.sync_joint_desired_deg(desired)
                if self._streaming and self.arm.is_connected() and not self.arm.motion_halted:
                    self.arm.advance_joint_command(timeout_ms=None)
                j = self.arm.read_joints_deg()
                if j is not None and self.on_joints is not None:
                    self.on_joints(tuple(j))
            except Exception:
                pass
            elapsed = time.monotonic() - t0
            wait = max(0.0, 0.02 - elapsed)
            self._stop.wait(wait)
