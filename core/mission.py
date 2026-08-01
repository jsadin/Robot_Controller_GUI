"""跨设备 Mission（任务组）模型与执行器。

合并原 ChassisTask：多星标巡航、回桩、停留、日历定时、暂停/恢复。
"""

from __future__ import annotations

import dataclasses
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, List, Optional, Sequence

from devices.arm.controller import ArmController
from devices.arm.sequences import default_poses_path, default_sequences_path, load_poses, load_sequences
from devices.camera import CameraBackend, save_snapshot, snapshot_path
from devices.chassis import ChassisClient, HermesError

# 经 ArmControlWorker 下发位姿（与动作组 UI 同路径，避免与 worker 抢写期望角）
ArmGotoFn = Callable[[Sequence[float]], None]
ArmAtTargetFn = Callable[[], bool]

# 与旧 ChassisTask 一致的回桩哨兵（迁移时用；运行时用 go_home 步骤）
HOME_POI = "__HOME__"


class MissionStatus:
    PENDING = "待执行"
    RUNNING = "执行中"
    PAUSED = "已暂停"
    DONE = "已完成"
    ABORTED = "已中断"


class ScheduleKind:
    NONE = "none"
    ONCE = "once"
    DAILY = "daily"


@dataclasses.dataclass
class MissionStep:
    kind: str  # navigate_poi | go_home | wait | run_sequence | snapshot
    params: dict = dataclasses.field(default_factory=dict)


@dataclasses.dataclass
class Mission:
    id: Optional[int]
    name: str
    steps: List[MissionStep]
    status: str = MissionStatus.PENDING
    cur_idx: int = 0
    reason: str = ""
    schedule_kind: str = ScheduleKind.NONE
    schedule_time: str = ""
    last_run_date: str = ""


def check_due_missions(now: datetime, missions: list) -> list:
    """返回此刻应定时触发的 mission.id 列表。"""
    due = []
    today = now.strftime("%Y-%m-%d")
    hhmm = now.strftime("%H:%M")
    for m in missions:
        if m.schedule_kind == ScheduleKind.NONE:
            continue
        if m.status in (MissionStatus.RUNNING, MissionStatus.PAUSED):
            continue
        if m.schedule_kind == ScheduleKind.DAILY:
            if m.schedule_time == hhmm and m.last_run_date != today:
                due.append(m.id)
        elif m.schedule_kind == ScheduleKind.ONCE:
            if not m.last_run_date and m.schedule_time:
                try:
                    fire = datetime.strptime(m.schedule_time, "%Y-%m-%d %H:%M")
                except ValueError:
                    continue
                if now >= fire:
                    due.append(m.id)
    return due


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
                    reason TEXT NOT NULL,
                    schedule_kind TEXT NOT NULL DEFAULT 'none',
                    schedule_time TEXT NOT NULL DEFAULT '',
                    last_run_date TEXT NOT NULL DEFAULT ''
                )
                """
            )
            cols = {r[1] for r in c.execute("PRAGMA table_info(missions)")}
            for name, decl in (
                ("schedule_kind", "TEXT NOT NULL DEFAULT 'none'"),
                ("schedule_time", "TEXT NOT NULL DEFAULT ''"),
                ("last_run_date", "TEXT NOT NULL DEFAULT ''"),
            ):
                if name not in cols:
                    c.execute(f"ALTER TABLE missions ADD COLUMN {name} {decl}")

    def list_missions(self) -> List[Mission]:
        import json

        with self._conn() as c:
            rows = c.execute(
                """
                SELECT id, name, steps_json, status, cur_idx, reason,
                       schedule_kind, schedule_time, last_run_date
                FROM missions ORDER BY id
                """
            ).fetchall()
        out = []
        for row in rows:
            (
                rid, name, steps_json, status, cur_idx, reason,
                schedule_kind, schedule_time, last_run_date,
            ) = row
            raw = json.loads(steps_json)
            steps = [MissionStep(kind=s["kind"], params=s.get("params") or {}) for s in raw]
            out.append(
                Mission(
                    rid, name, steps, status, cur_idx, reason,
                    schedule_kind or ScheduleKind.NONE,
                    schedule_time or "",
                    last_run_date or "",
                )
            )
        return out

    def get(self, mid: int) -> Optional[Mission]:
        for m in self.list_missions():
            if m.id == mid:
                return m
        return None

    def save(self, m: Mission) -> Mission:
        import json

        payload = json.dumps(
            [{"kind": s.kind, "params": s.params} for s in m.steps], ensure_ascii=False
        )
        with self._conn() as c:
            if m.id is None:
                cur = c.execute(
                    """
                    INSERT INTO missions(
                        name, steps_json, status, cur_idx, reason,
                        schedule_kind, schedule_time, last_run_date
                    ) VALUES(?,?,?,?,?,?,?,?)
                    """,
                    (
                        m.name, payload, m.status, m.cur_idx, m.reason,
                        m.schedule_kind, m.schedule_time, m.last_run_date,
                    ),
                )
                m.id = int(cur.lastrowid)
            else:
                c.execute(
                    """
                    UPDATE missions SET name=?, steps_json=?, status=?, cur_idx=?,
                        reason=?, schedule_kind=?, schedule_time=?, last_run_date=?
                    WHERE id=?
                    """,
                    (
                        m.name, payload, m.status, m.cur_idx, m.reason,
                        m.schedule_kind, m.schedule_time, m.last_run_date, m.id,
                    ),
                )
        return m

    def delete(self, mid: int) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM missions WHERE id=?", (mid,))


class MissionExecutor:
    """串行执行 Mission；支持暂停/恢复；可由 EStopBus 调用 abort()。"""

    def __init__(
        self,
        *,
        chassis: Optional[ChassisClient],
        arm: Optional[ArmController],
        camera: Optional[CameraBackend],
        data_dir: Path,
        on_status: Optional[Callable[[str], None]] = None,
        on_progress: Optional[Callable[[Mission], None]] = None,
        arm_goto: Optional[ArmGotoFn] = None,
        arm_at_target: Optional[ArmAtTargetFn] = None,
    ) -> None:
        self.chassis = chassis
        self.arm = arm
        self.camera = camera
        self.data_dir = data_dir
        self.on_status = on_status or (lambda _s: None)
        self.on_progress = on_progress or (lambda _m: None)
        # 优先走 UI 同款 worker；未注入时回退 controller 阻塞接口
        self.arm_goto = arm_goto
        self.arm_at_target = arm_at_target
        self._abort = threading.Event()
        self._pause = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._current: Optional[Mission] = None
        self._store: Optional[MissionStore] = None

    def abort(self) -> None:
        self._pause.clear()
        self._abort.set()
        if self._current is not None:
            self._current.status = MissionStatus.ABORTED
            if not self._current.reason:
                self._current.reason = "急停/中断"
        self._cancel_chassis()

    def pause(self) -> None:
        if not self.is_running():
            return
        self._pause.set()
        self._cancel_chassis()

    def _cancel_chassis(self) -> None:
        if self.chassis is not None:
            try:
                self.chassis.cancel_current_action()
            except HermesError:
                pass

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def current(self) -> Optional[Mission]:
        return self._current

    def start(
        self,
        mission: Mission,
        store: Optional[MissionStore] = None,
        *,
        resume: bool = False,
    ) -> None:
        if self.is_running():
            raise RuntimeError("已有任务组在执行")
        if not mission.steps:
            raise RuntimeError("任务组无步骤")
        self._abort.clear()
        self._pause.clear()
        self._current = mission
        self._store = store
        mission.status = MissionStatus.RUNNING
        mission.reason = ""
        if not resume:
            mission.cur_idx = 0
        elif mission.cur_idx < 0:
            mission.cur_idx = 0
        if resume and mission.cur_idx >= len(mission.steps):
            mission.status = MissionStatus.DONE
            if store is not None:
                store.save(mission)
            self.on_progress(mission)
            return
        if store is not None:
            store.save(mission)

        def _run() -> None:
            try:
                self._execute(mission)
                if self._abort.is_set():
                    mission.status = MissionStatus.ABORTED
                    if not mission.reason:
                        mission.reason = "急停/中断"
                elif self._pause.is_set():
                    mission.status = MissionStatus.PAUSED
                    mission.reason = "已暂停"
                else:
                    mission.status = MissionStatus.DONE
                    mission.cur_idx = len(mission.steps)
                    mission.reason = ""
            except Exception as e:
                mission.status = MissionStatus.ABORTED
                mission.reason = str(e)
            finally:
                if store is not None:
                    try:
                        store.save(mission)
                    except Exception:
                        pass
                self.on_progress(mission)
                self.on_status(f"任务组 {mission.name}: {mission.status}")

        self._thread = threading.Thread(target=_run, name="mission_exec", daemon=True)
        self._thread.start()

    def _stop_requested(self) -> bool:
        return self._abort.is_set() or self._pause.is_set()

    def _notify(self, mission: Mission, msg: str) -> None:
        # 先落库再通知 UI，避免主线程 refresh 读到旧 cur_idx
        if self._store is not None:
            try:
                self._store.save(mission)
            except Exception:
                pass
        self.on_progress(mission)
        self.on_status(msg)

    def _sleep_interruptible(self, seconds: float) -> None:
        end = time.monotonic() + float(seconds)
        while time.monotonic() < end:
            if self._stop_requested():
                return
            time.sleep(0.1)

    def _execute(self, mission: Mission) -> None:
        total = len(mission.steps)
        for i in range(mission.cur_idx, total):
            if self._stop_requested():
                return
            mission.cur_idx = i
            step = mission.steps[i]
            label = _step_label(step)
            self._notify(mission, f"任务组步骤 {i + 1}/{total}: {label}")
            self._run_step(step)
            if self._stop_requested():
                return

    def _resolve_poi(self, p: dict):
        assert self.chassis is not None
        poi_id = str(p.get("poi_id") or "")
        poi_name = str(p.get("poi_name") or "")
        pois = self.chassis.list_pois()
        for poi in pois:
            if poi_id and str(poi.poi_id) == poi_id:
                return poi
        if poi_name:
            for poi in pois:
                if str(poi.name) == poi_name:
                    return poi
        raise RuntimeError(f"找不到星标: id={poi_id or '-'} name={poi_name or '-'}")

    def _run_step(self, step: MissionStep) -> None:
        kind = step.kind
        p = step.params
        if kind == "wait":
            self._sleep_interruptible(float(p.get("seconds", 1)))
            return
        if kind == "navigate_poi":
            if self.chassis is None:
                raise RuntimeError("无底盘（请先连接）")
            poi = self._resolve_poi(p)
            self.chassis.move_to_poi(poi)
            self._wait_chassis_idle()
            if self._stop_requested():
                return
            # MoveTo 不保证终点朝向：与星标「前往」/原 ChassisTask 一致，到点后补转
            try:
                self.on_status(
                    f"任务组到点补转：{getattr(poi, 'name', '') or poi.poi_id}"
                )
                self.chassis.rotate_to(float(poi.yaw or 0.0))
                self._wait_chassis_idle()
            except HermesError:
                pass
            if self._stop_requested():
                return
            dwell = float(p.get("dwell_s") or 0)
            if dwell > 0:
                self._sleep_interruptible(dwell)
            return
        if kind == "go_home":
            if self.chassis is None:
                raise RuntimeError("无底盘（请先连接）")
            self.chassis.go_home()
            self._wait_chassis_idle()
            if self._stop_requested():
                return
            dwell = float(p.get("dwell_s") or 0)
            if dwell > 0:
                self._sleep_interruptible(dwell)
            return
        if kind == "run_sequence":
            if self.arm is None or not self.arm.is_connected():
                raise RuntimeError("机械臂未连接")
            name = str(p.get("sequence") or "")
            snap_mode = str(p.get("snapshot_mode") or "none").strip().lower()
            if snap_mode not in ("none", "each_pose", "selected"):
                snap_mode = "none"
            snap_poses = {
                str(x).strip()
                for x in (p.get("snapshot_poses") or [])
                if str(x).strip()
            }
            seqs = load_sequences(default_sequences_path(self.data_dir))
            poses = load_poses(default_poses_path(self.data_dir))
            if name not in seqs:
                raise RuntimeError(f"动作组不存在: {name}")
            loop, loop_count, steps = seqs[name]
            if not steps:
                raise RuntimeError(f"动作组「{name}」无步骤")
            passes = int(loop_count) if loop else 1
            for pass_i in range(max(1, passes)):
                for si, (pose_name, delay) in enumerate(steps):
                    if self._stop_requested():
                        return
                    if pose_name not in poses:
                        raise RuntimeError(f"位姿不存在: {pose_name}")
                    self.on_status(
                        f"任务组动作组「{name}」"
                        f" 第{pass_i + 1}/{max(1, passes)}遍"
                        f" 点位 {si + 1}/{len(steps)}：{pose_name}"
                    )
                    ok = self._goto_arm_pose(poses[pose_name])
                    if self._stop_requested():
                        return
                    if not ok:
                        raise RuntimeError(f"机械臂前往「{pose_name}」超时未到位")
                    need_snap = snap_mode == "each_pose" or (
                        snap_mode == "selected" and pose_name in snap_poses
                    )
                    if need_snap:
                        tag = f"seq_{name}_p{si + 1}_{pose_name}"
                        if passes > 1:
                            tag = f"{tag}_r{pass_i + 1}"
                        self._do_snapshot(tag, raise_on_fail=False)
                        if self._stop_requested():
                            return
                    self._sleep_interruptible(float(delay))
            return
        if kind == "snapshot":
            self._do_snapshot("step", raise_on_fail=True)
            return
        raise RuntimeError(f"未知步骤: {kind}")

    def _do_snapshot(self, tag: str, *, raise_on_fail: bool = True) -> bool:
        """抓拍落盘。raise_on_fail=False 时失败只记状态、不中断任务组。"""
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(tag))[:80]
        if self.camera is None:
            msg = "抓拍失败：无摄像头"
            self.on_status(msg)
            if raise_on_fail:
                raise RuntimeError(msg)
            return False
        frame = self.camera.read_bgr()
        if frame is None:
            msg = "抓拍失败：无画面（请先打开摄像头）"
            self.on_status(msg)
            if raise_on_fail:
                raise RuntimeError(msg)
            return False
        now = datetime.now()
        path = snapshot_path(
            self.data_dir,
            f"mission_{now:%H%M%S}_{safe or 'snap'}.jpg",
            when=now,
        )
        try:
            save_snapshot(frame, path)
        except Exception as e:
            msg = f"抓拍失败：{e}"
            self.on_status(msg)
            if raise_on_fail:
                raise RuntimeError(msg) from e
            return False
        self.on_status(f"任务组已抓拍 {path}")
        return True

    def _goto_arm_pose(self, deg6: Sequence[float], timeout_s: float = 120.0) -> bool:
        """下发关节目标并等到到位（优先 ArmControlWorker，避免与流控线程互抢）。"""
        if self.arm_goto is not None and self.arm_at_target is not None:
            self.arm_goto(deg6)
            deadline = time.monotonic() + timeout_s
            while time.monotonic() < deadline:
                if self._stop_requested():
                    return False
                try:
                    if self.arm_at_target():
                        return True
                except Exception:
                    pass
                time.sleep(0.05)
            return False
        # 回退：无 worker 时用 controller 阻塞（单线程场景）
        assert self.arm is not None
        return bool(self.arm.set_joints_deg_blocking(deg6, timeout_s=timeout_s))

    def _wait_chassis_idle(self, timeout_s: float = 600.0) -> None:
        """阻塞等到当前底盘 action 结束。

        语义对齐原 ChassisTask / 星标前往：
        - ``get_current_action() is None`` 表示空闲；
        - 刚下发后可能短暂仍为 None（action 尚未建起），需跳过首拍；
        - 固件若返回嵌套 ``state.status``，需正确识别 finished/idle。
        """
        assert self.chassis is not None
        deadline = time.monotonic() + timeout_s
        just_started = True
        while time.monotonic() < deadline:
            if self._stop_requested():
                try:
                    self.chassis.cancel_current_action()
                except HermesError:
                    pass
                return
            try:
                act = self.chassis.get_current_action()
            except HermesError:
                time.sleep(0.5)
                continue

            if self._action_still_running(act):
                just_started = False
                time.sleep(0.4)
                continue

            # 空闲：下发后第一拍空闲先忽略（action 可能尚未建起）
            if just_started:
                just_started = False
                time.sleep(0.25)
                continue
            time.sleep(0.2)
            try:
                act2 = self.chassis.get_current_action()
            except HermesError:
                return
            if not self._action_still_running(act2):
                return
            time.sleep(0.3)
        raise TimeoutError("底盘导航/旋转超时")

    @staticmethod
    def _action_still_running(act) -> bool:
        """当前是否仍有未完成的 motion action。"""
        if act is None:
            return False
        if not isinstance(act, dict):
            return True
        st = act.get("state")
        if isinstance(st, dict):
            status = str(st.get("status") or st.get("state") or "").strip().lower()
        else:
            status = str(st or act.get("status") or "").strip().lower()
        if status in (
            "finished",
            "done",
            "idle",
            "succeeded",
            "success",
            "completed",
            "canceled",
            "cancelled",
            "failed",
            "fail",
        ):
            return False
        # 无明确结束态时：只要有 action 对象即视为进行中（与原 TaskExecutor 一致）
        return True


def _step_label(step: MissionStep) -> str:
    p = step.params or {}
    if step.kind == "navigate_poi":
        name = p.get("poi_name") or p.get("poi_id") or "?"
        dwell = float(p.get("dwell_s") or 0)
        return f"导航 {name}" + (f"（停留{dwell:g}s）" if dwell > 0 else "")
    if step.kind == "go_home":
        dwell = float(p.get("dwell_s") or 0)
        return "回充电桩" + (f"（停留{dwell:g}s）" if dwell > 0 else "")
    if step.kind == "wait":
        return f"等待 {float(p.get('seconds', 1)):g} 秒"
    if step.kind == "run_sequence":
        name = p.get("sequence") or "?"
        mode = str(p.get("snapshot_mode") or "none").strip().lower()
        if mode == "each_pose":
            return f"动作组 {name}（每位姿抓拍）"
        if mode == "selected":
            poses = [str(x) for x in (p.get("snapshot_poses") or []) if str(x).strip()]
            if poses:
                shown = ", ".join(poses[:3])
                if len(poses) > 3:
                    shown += "…"
                return f"动作组 {name}（抓拍: {shown}）"
            return f"动作组 {name}（勾选抓拍）"
        return f"动作组 {name}"
    if step.kind == "snapshot":
        return "摄像头抓拍"
    return step.kind


def step_summary(step: MissionStep) -> str:
    """供 UI 显示。"""
    return _step_label(step)
