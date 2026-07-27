# 迁移说明

## 旧仓只读源

| 旧路径 | 迁入 |
|--------|------|
| `SLAM/hermes/` | `devices/chassis/` |
| `SLAM/ui/` | `ui/`（扩展 Tab） |
| `SLAM/tasks/` | `tasks/`（ChassisTask） |
| `ES66/.../elite_teleop_gui/` | `devices/arm/` + `devices/camera/` |

旧目录 `SLAM/`、`ES66/` 保留作对照，**新功能只写新 monorepo 包**。

## 数据目录

默认 `~/.robot_controller/`：

- `tasks.db` — ChassisTask
- `missions.db` — Mission
- `arm_poses.json` / `arm_sequences.json`
- `snapshots/` — 抓拍

## PyQt

统一 **PyQt5**。机械臂原 PyQt6 UI 不复用窗口，仅复用无 Qt 逻辑。

## SDK

真机臂需本机可 `import elite_cs_sdk`。否则 `arm.kind: mock`。
