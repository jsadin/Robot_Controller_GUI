"""定时调度 (功能表 #12)。

无线程, 由外部 QTimer 周期调用 check_due(now, tasks)。返回此刻应触发的
任务 id 列表。触发后调用方应给任务打上 last_run_date 防重复。

单次(ONCE): schedule_time = "YYYY-MM-DD HH:MM", 到点(<=now)且未跑过则触发。
每日(DAILY): schedule_time = "HH:MM", 当天该分钟到达且今天没跑过则触发。

只触发当前空闲(PENDING/DONE/ABORTED)的任务; 正在跑或暂停的不重复触发。
"""

from __future__ import annotations

from datetime import datetime

from .model import ScheduleKind, Task, TaskStatus


def check_due(now: datetime, tasks: list) -> list:
    """返回应触发的 task.id 列表。"""
    due = []
    today = now.strftime("%Y-%m-%d")
    hhmm = now.strftime("%H:%M")
    for t in tasks:
        if t.schedule_kind == ScheduleKind.NONE:
            continue
        if t.status in (TaskStatus.RUNNING, TaskStatus.PAUSED):
            continue
        if t.schedule_kind == ScheduleKind.DAILY:
            # 每天 HH:MM; 今天这一分钟还没跑过
            if t.schedule_time == hhmm and t.last_run_date != today:
                due.append(t.id)
        elif t.schedule_kind == ScheduleKind.ONCE:
            # 到点(分钟精度)且从未触发
            if not t.last_run_date and t.schedule_time:
                try:
                    fire = datetime.strptime(t.schedule_time, "%Y-%m-%d %H:%M")
                except ValueError:
                    continue
                if now >= fire:
                    due.append(t.id)
    return due
