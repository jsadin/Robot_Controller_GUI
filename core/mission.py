"""跨设备 Mission（任务组）模型与执行器。"""

from __future__ import annotations

import dataclasses
import sqlite3
import threading
import time
from pathlib import Path
from typing import Callable, List, Optional

from devices.arm.controller import ArmController
from devices.arm.sequences import default_poses_path, default_sequences_path, load_poses, load_sequences
from devices.camera import CameraBackend, save_snapshot
from devices.chassis import ChassisClient, HermesError


class MissionStatus:
    PENDING = "待执行"
    RUNNING = "执行中"
    DONE = "已完成"
    ABORTED = "已中断"


@dataclasses.dataclass
class MissionStep:
    kind: str  # navigate_poi | wait | run_sequence | snapshot
    params: dict = dataclasses.field(default_factory=dict)


@dataclasses.dataclass
class Mission:
    id: Optional[int]
    name: str
    steps: List[MissionStep]
    status: str = MissionStatus.PENDING
    cur_idx: int = 0
    reason: str = ""


class MissionStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(str(self.db_path))

    def _init(self) -> None:
        with self._conn() as c:
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS missions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    steps_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    cur_idx INTEGER NOT NULL,
                    reason TEXT NOT NULL
                )
                """
            )

    def list_missions(self) -> List[Mission]:
        import json

        with self._conn() as c:
            rows = c.execute(
                "SELECT id, name, steps_json, status, cur_idx, reason FROM missions ORDER BY id"
            ).fetchall()
        out = []
        for rid, name, steps_json, status, cur_idx, reason in rows:
            raw = json.loads(steps_json)
            steps = [MissionStep(kind=s["kind"], params=s.get("params") or {}) for s in raw]
            out.append(Mission(rid, name, steps, status, cur_idx, reason))
        return out

    def save(self, m: Mission) -> Mission:
        import json

        payload = json.dumps(
            [{"kind": s.kind, "params": s.params} for s in m.steps], ensure_ascii=False
        )
        with self._conn() as c:
            if m.id is None:
                cur = c.execute(
                    "INSERT INTO missions(name, steps_json, status, cur_idx, reason) VALUES(?,?,?,?,?)",
                    (m.name, payload, m.status, m.cur_idx, m.reason),
                )
                m.id = int(cur.lastrowid)
            else:
                c.execute(
                    "UPDATE missions SET name=?, steps_json=?, status=?, cur_idx=?, reason=? WHERE id=?",
                    (m.name, payload, m.status, m.cur_idx, m.reason, m.id),
                )
        return m

    def delete(self, mid: int) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM missions WHERE id=?", (mid,))


class MissionExecutor:
    """串行执行 Mission；可由 EStopBus 调用 abort()。"""

    def __init__(
        self,
        *,
        chassis: Optional[ChassisClient],
        arm: Optional[ArmController],
        camera: Optional[CameraBackend],
        data_dir: Path,
        on_status: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.chassis = chassis
        self.arm = arm
        self.camera = camera
        self.data_dir = data_dir
        self.on_status = on_status or (lambda _s: None)
        self._abort = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._current: Optional[Mission] = None

    def abort(self) -> None:
        self._abort.set()
        if self._current is not None:
            self._current.status = MissionStatus.ABORTED
            self._current.reason = "急停/中断"

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, mission: Mission, store: Optional[MissionStore] = None) -> None:
        if self.is_running():
            raise RuntimeError("已有 Mission 在执行")
        self._abort.clear()
        self._current = mission
        mission.status = MissionStatus.RUNNING
        mission.reason = ""

        def _run() -> None:
            try:
                self._execute(mission)
                if not self._abort.is_set():
                    mission.status = MissionStatus.DONE
                    mission.cur_idx = len(mission.steps)
            except Exception as e:
                mission.status = MissionStatus.ABORTED
                mission.reason = str(e)
            finally:
                if store is not None:
                    store.save(mission)
                self.on_status(f"Mission {mission.name}: {mission.status}")

        self._thread = threading.Thread(target=_run, name="mission_exec", daemon=True)
        self._thread.start()

    def _execute(self, mission: Mission) -> None:
        for i in range(mission.cur_idx, len(mission.steps)):
            if self._abort.is_set():
                mission.status = MissionStatus.ABORTED
                return
            mission.cur_idx = i
            step = mission.steps[i]
            self.on_status(f"步骤 {i + 1}/{len(mission.steps)}: {step.kind}")
            self._run_step(step)

    def _run_step(self, step: MissionStep) -> None:
        kind = step.kind
        p = step.params
        if kind == "wait":
            sec = float(p.get("seconds", 1))
            end = time.monotonic() + sec
            while time.monotonic() < end:
                if self._abort.is_set():
                    return
                time.sleep(0.1)
            return
        if kind == "navigate_poi":
            if self.chassis is None:
                raise RuntimeError("无底盘")
            poi_id = str(p.get("poi_id") or "")
            if not poi_id:
                raise RuntimeError("navigate_poi 缺少 poi_id")
            self.chassis.move_to_poi(poi_id)
            self._wait_chassis_idle()
            return
        if kind == "run_sequence":
            if self.arm is None:
                raise RuntimeError("无机械臂")
            name = str(p.get("sequence") or "")
            seqs = load_sequences(default_sequences_path(self.data_dir))
            poses = load_poses(default_poses_path(self.data_dir))
            if name not in seqs:
                raise RuntimeError(f"动作组不存在: {name}")
            _loop, steps = seqs[name]
            for pose_name, delay in steps:
                if self._abort.is_set():
                    return
                if pose_name not in poses:
                    raise RuntimeError(f"位姿不存在: {pose_name}")
                self.arm.set_joints_deg_blocking(poses[pose_name])
                end = time.monotonic() + float(delay)
                while time.monotonic() < end:
                    if self._abort.is_set():
                        return
                    time.sleep(0.1)
            return
        if kind == "snapshot":
            if self.camera is None:
                raise RuntimeError("无摄像头")
            frame = self.camera.read_bgr()
            if frame is None:
                raise RuntimeError("无画面可抓拍")
            from datetime import datetime

            path = self.data_dir / "snapshots" / f"mission_{datetime.now():%Y%m%d_%H%M%S}.jpg"
            save_snapshot(frame, path)
            return
        raise RuntimeError(f"未知步骤: {kind}")

    def _wait_chassis_idle(self, timeout_s: float = 600.0) -> None:
        assert self.chassis is not None
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self._abort.is_set():
                try:
                    self.chassis.abort_action()
                except HermesError:
                    pass
                return
            try:
                act = self.chassis.get_current_action()
            except HermesError:
                time.sleep(0.5)
                continue
            # 无当前动作或已结束
            state = ""
            if isinstance(act, dict):
                state = str(act.get("state") or act.get("status") or "")
            if not act or state.lower() in ("finished", "done", "idle", ""):
                # 再确认一次：有些固件返回 None 表示空闲
                if not act or "FAIL" not in state.upper():
                    time.sleep(0.3)
                    return
            time.sleep(0.4)
        raise TimeoutError("底盘导航超时")
