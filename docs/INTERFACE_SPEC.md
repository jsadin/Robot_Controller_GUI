# 接口规范（Phase0 锁版）

## 1. 配置

- 主文件：[`config/devices.yaml`](../config/devices.yaml)
- 本地覆盖（可选，gitignore）：`config/devices.local.yaml`
- 加载入口：`devices.config_loader.load_devices_config()`
- 环境变量：`ROBOT_CAMERA_PASSWORD` 覆盖摄像头密码；`ROBOT_CONFIG` 指定配置路径

## 2. 设备协议（无 Qt）

### ChassisClient

迁自 `SLAM/hermes`，包路径 `devices.chassis`。

- `ping()` / 构造即连
- `get_pose()` / `get_power_status()` / `get_health()` / `get_health_summary()`
- 地图/POI/墙/运动 action API（保持 Hermes 方法名）
- `set_emergency_stop(on: bool)` / `abort_action()`

### ArmController

门面：`devices.arm.controller.ArmController`（对齐原 `TeleopController`）。

- `connect()` / `disconnect()` / `is_connected()`
- `read_joints_deg()` / `sync_joint_desired_deg()` / `advance_joint_command()`
- `emergency_stop()` / `clear_emergency_stop()`
- 预留：`brake_release()` / `start_freedrive()` / `stop_freedrive()` → 默认 `NotImplemented` 或 no-op 返回 False
- 后端：`mock` | `elite_cs`（需 `elite_cs_sdk`）

控制循环：**禁止**在 UI 主线程紧密调用；由 `ui/arm_worker.py` 线程按 ~50Hz 调用 `advance_joint_command`。

### CameraBackend

- `open()` / `close()` / `read_bgr() -> Optional[ndarray]`
- kind：`hikvision` | `rtsp` | `usb` | `mock`

### RangingBackend

- `get_distance_m() -> Optional[float]`
- `enabled=false` 时为 stub，恒返回 `None`

## 3. 统一状态与急停

```text
DeviceId = chassis | arm | camera | ranging
ConnectionState = disconnected | connecting | connected | error
AlarmItem = {device, code, message, level}
```

`EStopBus.trigger()`：

1. 停止 Mission / ChassisTask 执行器
2. `chassis.set_emergency_stop(True)`（若已连接）
3. `arm.emergency_stop()`（若已连接）
4. 广播 `on_estop(True)` 回调

`EStopBus.release()`：解除软件闩锁；底盘 `set_emergency_stop(False)`；臂 `clear_emergency_stop()`（硬件示教器急停需人工确认）。

## 4. UI 约定

- 面板只发 `pyqtSignal`，不直接 `requests` / `elite_cs_sdk`
- 主窗口持有设备单例
- 主题：`ui/theme.py`（SLAM 深色）
- Tab：星标 | 任务组(Mission) | 诊断（遥控/臂/视觉在分屏工作区）

## 5. Mission（已合并原 ChassisTask）

有序步骤：`navigate_poi`（可停留）| `go_home` | `wait` | `run_sequence` | `snapshot`

串行执行；暂停/恢复；日历定时（单次/每日）；全局急停打断；SQLite 落库于数据目录。旧 `tasks.db` 启动时一次性导入。

## 6. 禁止

- UI 散落裸 URL / SDK import
- 底盘与臂两套急停互不影响
- 未完成 Phase0 冒烟前做大 UI 功能
