"""任务执行器 (状态机)。

底盘同一时刻只能执行一个 action, 所以任务严格串行: 同一时刻只有一个
"执行中"任务。执行器由外部心跳(MainWindow.poll 每 200ms)驱动 tick()。

tick 逻辑(针对当前执行中任务):
    看 client.get_current_action():
      - 非 None  -> 机器人还在前往当前航点, 等待
      - None     -> 当前航点已到达(实测空闲返回 None):
                      cur_idx += 1 落库
                      若还有航点 -> 前往下一个
                      否则       -> 任务完成
执行器不直接碰 UI; 通过回调 on_change(task) 通知外层刷新显示。

断点续接: cur_idx 落库, pause 只是 cancel + 置暂停态; resume 从 cur_idx
继续, 已走过航点不重复。
"""

from __future__ import annotations

from typing import Callable, Optional

from .model import HOME_POI, Task, TaskStatus


class TaskExecutor:
    def __init__(self, store, pois_provider: Callable,
                 on_change: Optional[Callable] = None):
        """
        store: TaskStore, 用于落库进度/状态。
        pois_provider: 无参回调, 返回当前 POI 列表(对象含 .poi_id/.x/.y/.yaw)。
                       执行器据 poi_id 查坐标下发导航。
        on_change: 任务状态变化时回调 on_change(task), 供 UI 刷新。
        """
        self.store = store
        self.pois_provider = pois_provider
        self.on_change = on_change
        self._running_id: Optional[int] = None   # 当前执行中任务 id
        self._just_started = False                # 刚下发动作, 跳过一次"完成"判定
        self._rotating = False                    # 到点补转(旋转到星标朝向)进行中
        self._dwell_until = 0.0                   # 停留结束时刻(monotonic), 0=未在停留
        import time
        self._now = time.monotonic               # 可替换, 便于测试

    # ---- 对外控制 ----

    def start(self, task: Task, client) -> None:
        """开始一个任务(从头)。若已有任务在跑, 拒绝(严格串行)。"""
        if self._running_id is not None:
            return
        if not task.poi_ids:
            task.status = TaskStatus.ABORTED
            task.reason = "任务无航点"
            self.store.update(task)
            self._notify(task)
            return
        task.cur_idx = 0
        task.status = TaskStatus.RUNNING
        task.reason = ""
        self.store.update(task)
        self._running_id = task.id
        self._dwell_until = 0.0
        self._rotating = False
        self._goto_current(task, client)
        self._notify(task)

    def resume(self, task: Task, client) -> None:
        """从断点(cur_idx)继续。

        若 cur_idx 已越界(任务实际已跑完/已完成态被点恢复), 视为已完成,
        不再下发导航, 避免 IndexError。
        """
        if self._running_id is not None:
            return
        if not task.poi_ids:
            task.status = TaskStatus.ABORTED
            task.reason = "任务无航点"
            self.store.update(task)
            self._notify(task)
            return
        if task.cur_idx >= len(task.poi_ids):
            # 已走完所有航点, 直接标完成
            task.status = TaskStatus.DONE
            self.store.update(task)
            self._notify(task)
            return
        if task.cur_idx < 0:
            task.cur_idx = 0
        task.status = TaskStatus.RUNNING
        task.reason = ""
        self.store.update(task)
        self._running_id = task.id
        self._rotating = False
        self._goto_current(task, client)
        self._notify(task)

    def pause(self, client) -> None:
        """暂停当前任务: 停车, 进度已落库。"""
        if self._running_id is None:
            return
        task = self.store.get(self._running_id)
        self._safe_cancel(client)
        self._dwell_until = 0.0
        self._rotating = False
        if task:
            task.status = TaskStatus.PAUSED
            self.store.update(task)
            self._notify(task)
        self._running_id = None

    def abort(self, client, reason: str = "手动中断") -> None:
        if self._running_id is None:
            return
        task = self.store.get(self._running_id)
        self._safe_cancel(client)
        self._dwell_until = 0.0
        self._rotating = False
        if task:
            task.status = TaskStatus.ABORTED
            task.reason = reason
            self.store.update(task)
            self._notify(task)
        self._running_id = None

    # ---- 心跳 ----

    def tick(self, client) -> None:
        """由外部定时调用。推进当前执行中任务。"""
        if self._running_id is None:
            return
        task = self.store.get(self._running_id)
        if task is None or task.status != TaskStatus.RUNNING:
            self._running_id = None
            return
        # 正在某航点停留中: 等够时间再前往下一个
        if self._dwell_until:
            if self._now() < self._dwell_until:
                return
            self._dwell_until = 0.0
            self._advance(task, client)
            return
        try:
            act = client.get_current_action()
        except Exception as e:  # noqa: BLE001 网络抖动, 下拍再试
            return
        if act is not None:
            return  # 还在前往当前航点 / 正在到点补转旋转
        # 当前航点已到达。刚下发动作时, 底盘可能还没把 action 建起来,
        # 跳过这一拍避免误判完成。
        if self._just_started:
            self._just_started = False
            return
        # 阶段1: 到点补转 —— 与"单独前往"一致, 到达后原地转到星标朝向。
        # MoveToAction 不保证终点朝向, 故到点后补一个 RotateToAction。
        if not self._rotating:
            poi = self._arrival_poi(task)
            if poi is not None:
                try:
                    client.rotate_to(poi.yaw or 0.0)
                    self._rotating = True
                    self._just_started = True   # 跳过一拍, 等旋转 action 建立
                    return
                except Exception:  # noqa: BLE001 补转失败不致命, 继续后续流程
                    pass
        # 阶段2: 旋转完成(或无需旋转) -> 停留/推进
        self._rotating = False
        dwell = task.dwell_at(task.cur_idx)
        if dwell > 0:
            self._dwell_until = self._now() + dwell
            self._notify(task)   # 让 UI 显示"停留中"(可选)
            return
        self._advance(task, client)

    def _arrival_poi(self, task: Task):
        """当前已到达航点对应的 POI(用于到点补转)。回桩哨兵或找不到返回 None。"""
        if task.cur_idx >= len(task.poi_ids):
            return None
        pid = task.poi_ids[task.cur_idx]
        if pid == HOME_POI:
            return None
        return self._find_poi(pid)

    def _advance(self, task: Task, client) -> None:
        """当前航点完成, 推进到下一个或标完成。"""
        task.cur_idx += 1
        self.store.update(task)
        if task.cur_idx >= len(task.poi_ids):
            task.status = TaskStatus.DONE
            self.store.update(task)
            self._running_id = None
            self._notify(task)
        else:
            self._goto_current(task, client)
            self._notify(task)

    # ---- 内部 ----

    def current_target_poi_id(self) -> Optional[str]:
        """当前正前往的航点 poi_id(供 UI 高亮), 无则 None。回桩哨兵不高亮。"""
        if self._running_id is None:
            return None
        task = self.store.get(self._running_id)
        if task and task.cur_idx < len(task.poi_ids):
            pid = task.poi_ids[task.cur_idx]
            return None if pid == HOME_POI else pid
        return None

    def _goto_current(self, task: Task, client) -> None:
        """前往 cur_idx 指向的航点。越界则视为完成, 找不到 POI 则中断。"""
        if task.cur_idx >= len(task.poi_ids):
            task.status = TaskStatus.DONE
            self.store.update(task)
            self._running_id = None
            self._notify(task)
            return
        pid = task.poi_ids[task.cur_idx]
        # 回桩哨兵: 调 go_home, 不查 POI
        if pid == HOME_POI:
            try:
                client.go_home()
                self._just_started = True
            except Exception as e:  # noqa: BLE001
                task.status = TaskStatus.ABORTED
                task.reason = f"回桩下发失败: {e}"
                self.store.update(task)
                self._running_id = None
                self._notify(task)
            return
        poi = self._find_poi(pid)
        if poi is None:
            task.status = TaskStatus.ABORTED
            task.reason = f"航点 {pid} 不存在(星标可能已删除)"
            self.store.update(task)
            self._running_id = None
            self._notify(task)
            return
        try:
            client.move_to_poi(poi)
            self._just_started = True
        except Exception as e:  # noqa: BLE001
            task.status = TaskStatus.ABORTED
            task.reason = f"导航下发失败: {e}"
            self.store.update(task)
            self._running_id = None
            self._notify(task)

    def _find_poi(self, poi_id: str):
        for p in self.pois_provider() or []:
            if str(p.poi_id) == str(poi_id):
                return p
        return None

    def _safe_cancel(self, client) -> None:
        try:
            client.cancel_current_action()
        except Exception:  # noqa: BLE001 已无 action 时忽略
            pass

    def _notify(self, task: Task) -> None:
        if self.on_change:
            self.on_change(task)
