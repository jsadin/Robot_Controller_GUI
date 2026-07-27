"""任务数据模型。

一个任务 = 有序的星标(POI)序列 + 状态 + 当前进度 + 可选定时配置。
"""

from __future__ import annotations

import dataclasses
from typing import Optional


class TaskStatus:
    """任务状态枚举(用字符串, 便于落库与显示)。"""

    PENDING = "待执行"
    RUNNING = "执行中"
    PAUSED = "已暂停"
    DONE = "已完成"
    ABORTED = "已中断"


class ScheduleKind:
    """定时类型。"""

    NONE = "none"      # 不定时, 手动执行
    ONCE = "once"      # 单次: 到某个时间戳执行一次
    DAILY = "daily"    # 每日: 每天 HH:MM 执行


# 特殊航点哨兵: 代表"回充电桩"。作为 poi_id 出现在 poi_ids 序列里,
# 执行器遇到它调 go_home() 而非 move_to_poi。
HOME_POI = "__HOME__"


@dataclasses.dataclass
class Task:
    """一个调度任务。

    poi_ids: 有序航点(星标 id)列表, 机器人依次前往。特殊值 HOME_POI 表示回桩。
    dwells:  与 poi_ids 等长的停留秒数列表, 到达每个航点后停留 dwells[i] 秒
             再前往下一个。缺省补 0(不停留)。
    cur_idx: 当前执行到第几个航点(0 基)。落库支持断点续接 —— 中断后
             resume 从 cur_idx 继续, 已走过的航点跳过。
    schedule_kind/schedule_time:
        NONE -> 无
        ONCE -> schedule_time 是 "YYYY-MM-DD HH:MM" (本地时间)
        DAILY-> schedule_time 是 "HH:MM"
    last_run_date: 最近触发日期 "YYYY-MM-DD", 防止每日任务一天内重复触发,
                   或单次任务重复触发。
    """

    id: Optional[int]            # 数据库主键, 新建时 None
    name: str
    poi_ids: list                # list[str], 含可能的 HOME_POI
    dwells: list = dataclasses.field(default_factory=list)  # list[int] 停留秒
    status: str = TaskStatus.PENDING
    cur_idx: int = 0
    schedule_kind: str = ScheduleKind.NONE
    schedule_time: str = ""
    last_run_date: str = ""
    reason: str = ""             # 中断原因

    def dwell_at(self, idx: int) -> int:
        """第 idx 个航点的停留秒数(越界/缺省为 0)。"""
        return self.dwells[idx] if 0 <= idx < len(self.dwells) else 0

    def progress(self) -> str:
        """进度文字, 如 '2/3'。"""
        return f"{min(self.cur_idx, len(self.poi_ids))}/{len(self.poi_ids)}"

    def is_active(self) -> bool:
        return self.status in (TaskStatus.RUNNING, TaskStatus.PAUSED)
