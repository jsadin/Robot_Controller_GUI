"""测距联动自动变焦（第一阶段：赶到机械止挡，不读绝对倍率）。"""

from __future__ import annotations

from typing import Literal, Optional

Gear = Literal["min", "max"]
Action = Literal["none", "start_max", "start_min", "hold_max", "hold_min"]


def want_max_zoom(distance_m: Optional[float], near_m: float = 1.0) -> bool:
    """有效距离大于 near_m → 最长焦；≤ near_m → 最短焦。None 不在此函数里当过远。"""
    if distance_m is None:
        return True
    try:
        d = float(distance_m)
    except (TypeError, ValueError):
        return True
    return d > float(near_m)


def ranging_too_far(err: Optional[str]) -> bool:
    text = (err or "").strip()
    if not text:
        return False
    return "0x7FFFFFFF" in text or "无有效距离" in text or "过远" in text


class AutoZoomController:
    """只在目标档位变化时发出一次 start_*；行程中返回 hold_* 以便续发连续变倍。"""

    def __init__(self, *, near_m: float = 1.0) -> None:
        self.near_m = float(near_m)
        self.enabled = False
        self._gear: Optional[Gear] = None
        self._pending: Optional[Gear] = None
        self._seen_valid = False

    def reset(self) -> None:
        self._gear = None
        self._pending = None
        self._seen_valid = False

    def set_enabled(self, on: bool) -> None:
        self.enabled = bool(on)
        if not self.enabled:
            self.reset()

    def tick(self, distance_m: Optional[float], *, too_far: bool = False) -> Action:
        if not self.enabled:
            return "none"
        target = self._resolve_target(distance_m, too_far=too_far)
        if target is None:
            if self._pending is not None:
                return "hold_max" if self._pending == "max" else "hold_min"
            return "none"
        if self._pending is not None:
            return "hold_max" if self._pending == "max" else "hold_min"
        if self._gear == target:
            return "none"
        self._pending = target
        return "start_max" if target == "max" else "start_min"

    def _resolve_target(self, distance_m: Optional[float], *, too_far: bool) -> Optional[Gear]:
        if distance_m is not None:
            try:
                d = float(distance_m)
            except (TypeError, ValueError):
                d = None
            if d is not None:
                self._seen_valid = True
                return "max" if d > self.near_m else "min"
        # 启动后尚未出过有效读数：不要把 None 当成过远，否则会先拉长焦再被近距打回。
        if not self._seen_valid:
            return None
        if too_far:
            return "max"
        return None

    def on_travel_done(self) -> None:
        if self._pending is not None:
            self._gear = self._pending
            self._pending = None
