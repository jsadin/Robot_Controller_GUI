"""任务持久化 (SQLite)。

任务队列与进度落库, 程序重启后能恢复; cur_idx 落库支持断点续接。
poi_ids 以逗号分隔字符串存(POI id 是 UUID, 不含逗号)。
"""

from __future__ import annotations

import os
import sqlite3
from typing import Optional

from .model import ScheduleKind, Task, TaskStatus


def _default_db_path() -> str:
    """任务库放用户主目录下, 避免打包 exe 在只读工作目录写库失败
    (attempt to write a readonly database)。"""
    base = os.path.join(os.path.expanduser("~"), ".robot_controller")
    try:
        os.makedirs(base, exist_ok=True)
        return os.path.join(base, "tasks.db")
    except OSError:
        return "tasks.db"   # 退回当前目录(开发态)


DEFAULT_DB = _default_db_path()


class TaskStore:
    def __init__(self, path: str = DEFAULT_DB):
        # check_same_thread=False: 允许 Qt 主线程与可能的回调共用; 本项目单线程使用。
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                name          TEXT NOT NULL,
                poi_ids       TEXT NOT NULL,
                status        TEXT NOT NULL,
                cur_idx       INTEGER NOT NULL DEFAULT 0,
                schedule_kind TEXT NOT NULL DEFAULT 'none',
                schedule_time TEXT NOT NULL DEFAULT '',
                last_run_date TEXT NOT NULL DEFAULT '',
                reason        TEXT NOT NULL DEFAULT '',
                dwells        TEXT NOT NULL DEFAULT ''
            )
            """
        )
        # 迁移已有库: 缺 dwells 列则补上
        cols = [r[1] for r in self.conn.execute("PRAGMA table_info(tasks)")]
        if "dwells" not in cols:
            self.conn.execute(
                "ALTER TABLE tasks ADD COLUMN dwells TEXT NOT NULL DEFAULT ''"
            )
        self.conn.commit()

    # ---- 行 <-> Task ----

    @staticmethod
    def _row_to_task(r: sqlite3.Row) -> Task:
        dwells_raw = r["dwells"] if "dwells" in r.keys() else ""
        dwells = [int(x) for x in dwells_raw.split(",") if x] if dwells_raw else []
        return Task(
            id=r["id"],
            name=r["name"],
            poi_ids=r["poi_ids"].split(",") if r["poi_ids"] else [],
            dwells=dwells,
            status=r["status"],
            cur_idx=r["cur_idx"],
            schedule_kind=r["schedule_kind"],
            schedule_time=r["schedule_time"],
            last_run_date=r["last_run_date"],
            reason=r["reason"],
        )

    # ---- CRUD ----

    def list_tasks(self) -> list:
        cur = self.conn.execute("SELECT * FROM tasks ORDER BY id")
        return [self._row_to_task(r) for r in cur.fetchall()]

    def get(self, task_id: int) -> Optional[Task]:
        cur = self.conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,))
        r = cur.fetchone()
        return self._row_to_task(r) if r else None

    def add(self, task: Task) -> int:
        cur = self.conn.execute(
            """INSERT INTO tasks
               (name, poi_ids, status, cur_idx, schedule_kind,
                schedule_time, last_run_date, reason, dwells)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (task.name, ",".join(task.poi_ids), task.status, task.cur_idx,
             task.schedule_kind, task.schedule_time, task.last_run_date,
             task.reason, ",".join(str(int(d)) for d in task.dwells)),
        )
        self.conn.commit()
        task.id = cur.lastrowid
        return task.id

    def update(self, task: Task) -> None:
        self.conn.execute(
            """UPDATE tasks SET name=?, poi_ids=?, status=?, cur_idx=?,
               schedule_kind=?, schedule_time=?, last_run_date=?, reason=?,
               dwells=? WHERE id=?""",
            (task.name, ",".join(task.poi_ids), task.status, task.cur_idx,
             task.schedule_kind, task.schedule_time, task.last_run_date,
             task.reason, ",".join(str(int(d)) for d in task.dwells), task.id),
        )
        self.conn.commit()

    def delete(self, task_id: int) -> None:
        self.conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
        self.conn.commit()
