"""后期数据分析模块端口（功能表 stub）。"""

from __future__ import annotations

from typing import Any, Dict


class AnalyticsPort:
    def push_event(self, event_type: str, payload: Dict[str, Any]) -> bool:
        _ = (event_type, payload)
        return False

    def is_available(self) -> bool:
        return False
