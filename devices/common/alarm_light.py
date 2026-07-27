"""外部报警灯预留接口（功能表总体 #6）。"""

from __future__ import annotations


class AlarmLightPort:
    """v1 stub：不驱动硬件。"""

    def set_alarm(self, on: bool) -> bool:
        _ = on
        return False

    def is_available(self) -> bool:
        return False
