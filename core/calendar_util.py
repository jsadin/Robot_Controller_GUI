"""ChassisTask 巡检日历辅助（功能表底盘 #10）。"""

from __future__ import annotations

from datetime import date, datetime
from typing import List, Tuple

from tasks.model import ScheduleKind, Task


def parse_schedule_slots(task: Task) -> List[Tuple[str, str]]:
    """返回 [(日期或每日, 时间)] 便于 UI 展示。"""
    if task.schedule_kind == ScheduleKind.NONE or not task.schedule_time:
        return []
    if task.schedule_kind == ScheduleKind.ONCE:
        # "YYYY-MM-DD HH:MM"
        parts = task.schedule_time.strip().split()
        if len(parts) == 2:
            return [(parts[0], parts[1])]
        return [(task.schedule_time, "")]
    if task.schedule_kind == ScheduleKind.DAILY:
        return [("每日", task.schedule_time.strip())]
    return []


def next_due_hint(task: Task, now: datetime | None = None) -> str:
    now = now or datetime.now()
    if task.schedule_kind == ScheduleKind.DAILY and task.schedule_time:
        try:
            hh, mm = task.schedule_time.split(":")
            candidate = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
            if candidate <= now:
                from datetime import timedelta

                candidate = candidate + timedelta(days=1)
            return candidate.strftime("%Y-%m-%d %H:%M")
        except Exception:
            return task.schedule_time
    if task.schedule_kind == ScheduleKind.ONCE:
        return task.schedule_time
    return ""


def calendar_day_tasks(tasks: List[Task], day: date) -> List[Task]:
    """筛出某日相关的 once 任务，或全部 daily。"""
    out = []
    ds = day.isoformat()
    for t in tasks:
        if t.schedule_kind == ScheduleKind.DAILY:
            out.append(t)
        elif t.schedule_kind == ScheduleKind.ONCE and t.schedule_time.startswith(ds):
            out.append(t)
    return out
