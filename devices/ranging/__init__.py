"""激光测距模块预留。"""

from __future__ import annotations

from typing import Optional

from devices.config_loader import DevicesConfig, RangingCfg


class RangingBackend:
    def __init__(self, cfg: RangingCfg) -> None:
        self._cfg = cfg

    @property
    def enabled(self) -> bool:
        return bool(self._cfg.enabled)

    def connect(self) -> bool:
        return False

    def close(self) -> None:
        return None

    def get_distance_m(self) -> Optional[float]:
        """未接入硬件时恒返回 None。"""
        return None


def build_ranging(cfg: DevicesConfig) -> RangingBackend:
    return RangingBackend(cfg.ranging)
