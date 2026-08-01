"""将旧 ChassisTask（tasks.db）一次性迁移到 Mission。"""

from __future__ import annotations

from pathlib import Path

from core.mission import HOME_POI, Mission, MissionStatus, MissionStep, MissionStore


def migrate_chassis_tasks_to_missions(
    data_dir: Path,
    mission_store: MissionStore,
    tasks_db: Path | None = None,
) -> int:
    """若尚未迁移且存在 tasks.db，则导入为任务组。返回迁移条数。"""
    marker = data_dir / ".tasks_migrated_to_missions"
    if marker.is_file():
        return 0
    db = tasks_db
    if db is None:
        db = Path.home() / ".robot_controller" / "tasks.db"
    if not db.is_file():
        marker.write_text("no-tasks-db\n", encoding="utf-8")
        return 0

    try:
        from tasks.store import TaskStore
        from tasks.model import HOME_POI as TASK_HOME
    except Exception:
        marker.write_text("import-failed\n", encoding="utf-8")
        return 0

    store = TaskStore(str(db))
    tasks = store.list_tasks()
    n = 0
    for t in tasks:
        steps: list[MissionStep] = []
        for i, pid in enumerate(t.poi_ids):
            dwell = int(t.dwell_at(i) or 0)
            if str(pid) == TASK_HOME or str(pid) == HOME_POI:
                steps.append(MissionStep("go_home", {"dwell_s": dwell}))
            else:
                steps.append(
                    MissionStep(
                        "navigate_poi",
                        {"poi_id": str(pid), "poi_name": "", "dwell_s": dwell},
                    )
                )
        if not steps:
            continue
        status = t.status
        if status not in (
            MissionStatus.PENDING,
            MissionStatus.RUNNING,
            MissionStatus.PAUSED,
            MissionStatus.DONE,
            MissionStatus.ABORTED,
        ):
            status = MissionStatus.PENDING
        # 执行中的旧任务迁入后改为暂停，避免幽灵执行
        if status == MissionStatus.RUNNING:
            status = MissionStatus.PAUSED
        m = Mission(
            id=None,
            name=f"{t.name}（自巡检任务导入）",
            steps=steps,
            status=status,
            cur_idx=min(int(t.cur_idx or 0), len(steps)),
            reason=t.reason or "",
            schedule_kind=t.schedule_kind or "none",
            schedule_time=t.schedule_time or "",
            last_run_date=t.last_run_date or "",
        )
        mission_store.save(m)
        n += 1

    marker.write_text(f"migrated={n}\n", encoding="utf-8")
    return n
