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
- `get_pose()` / `get_power_status()` / `get_health()` / `get_health_items()`
- 地图/POI/墙/运动 action API（保持 Hermes 方法名）
- `set_emergency_stop(on: bool)` / `abort_action()`

综合健康由上层 `DiagnosisAggregator.collect_with_summary()` 产出（非底盘单接口）。

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
- `last_frame_ts` / `frame_age_s()`：供诊断判定断流（无新帧超时）
- kind：`hikvision` | `rtsp` | `usb` | `mock`
- 海康变焦（ISAPI，不依赖 HCNetSDK）：`zoom_start` / `ptz_stop`
  - 预览默认子码流 `/Streaming/Channels/102`（低延迟）；抓拍走 ISAPI 主码流 JPEG
  - 配置：`http_port`（默认 80）、`ptz_channel`、`zoom_speed`
  - 自动变焦（默认开，可取消）：测距 ``d ≤ auto_zoom_near_m``（默认 1.0m / 1000mm）最短焦；有效 ``d > 1000mm`` 或测距仪回过远哨兵最长焦。启动后未出有效读数不动作。行程中续发连续变倍。手动按住变焦±会退出自动。

### RangingBackend

- `get_distance_m() -> Optional[float]`
- `enabled=false` 或 `kind=stub` 时恒返回 `None`
- `kind=elite_cabinet_rs485`：柜体 HF MODBUS-RTU
  - 机械臂已连接且配置了 ``ranging.ssh_password``：SSH 起 Python 桥读 ``/dev/ttyBoard``（不向 30001 发脚本，以免打断外部控制）
  - 机械臂已连接但无 SSH 密码：30001 ``sec`` 读 485，用 ``%`` 移位写入布尔寄存器（不用 ``&``）
  - 未接臂：30001 ``def`` + socket

## 3. 统一状态与急停

```text
DeviceId = chassis | arm | camera | ranging
ConnectionState = disconnected | connecting | connected | error
AlarmLevel = info | warn | error | critical
AlarmItem = {device, code, message, level}
DeviceStatus = {device, state, detail, alarms, metrics, ok}
OverallHealth = ok | degraded | fault
HealthSummary = {overall, fault_count, warn_count, generated_at}
```

### DiagnosisAggregator

- `collect()` / `collect_with_summary() -> (List[DeviceStatus], HealthSummary)`
- `collect_snapshot() -> dict`（JSON 可序列化，供日志导出）
- 底盘：health 级别映射、电量、定位质量、当前 action、急停/雷达
- 臂：连接、流控、软件急停闩锁、关节可读
- 相机：分辨率、帧龄；超时无新帧 → `STALE_FRAME`
- UI 约 2s 随 `_poll_health` 自动刷新诊断 Tab

### AppLog（`core/app_log.py`）

- `setup_logging()` → `~/.robot_controller/app.log`（RotatingFileHandler）
- 内存环形缓冲；`log_info` / `log_warn` / `log_error` / `get_recent`
- `export_bundle(path, snapshot)` → zip：`app.log`、`crash.log`（若有）、`diagnosis_snapshot.json`、`status_tail.txt`
- 诊断面板「导出日志」触发

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
