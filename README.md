# 机器人控制器整合版（Robot Controller GUI）

以 **SLAMTEC Hermes 底盘上位机** 为主壳（PyQt5），单进程整合 **ES66 机械臂**、**摄像头** 与预留测距/报警接口。

## 快速开始

```bash
cd Robot_Controller_GUI
pip install -r requirements.txt
# 冒烟（Mock，无需真机）
python -m unittest tests.smoke.test_phase0 -v
# 可选连通检查
python scripts/check_all_devices.py
# 启动 GUI（配置见 config/devices.yaml；连接对话框可取消以离线进入）
python -m ui.main_window
# 或强制离线
python -m ui.main_window --offline
```

## 配置

编辑 [`config/devices.yaml`](config/devices.yaml)：

| 设备 | 默认 |
|------|------|
| 底盘 | 192.168.11.1:1448 |
| 机械臂 | 192.168.11.150（`kind: mock` 或 `elite_cs`） |
| 摄像头 | 192.168.11.101 |
| PC local_ip | 臂 reverse socket 回连地址 |

本地覆盖（勿提交）：`config/devices.local.yaml`  
相机密码：环境变量 `ROBOT_CAMERA_PASSWORD`

数据目录：`~/.robot_controller/`

## 文档

- [功能映射](docs/FEATURE_MAP.md)
- [接口规范](docs/INTERFACE_SPEC.md)
- [迁移说明](docs/MIGRATION.md)

## 旧仓

`SLAM/`、`ES66/` 为只读迁移源，新功能请写本仓库包（`devices/` / `ui/` / `core/`）。

## 打包（可选）

```bash
pyinstaller --noconfirm --windowed --name RobotControllerGUI ui/main_window.py
```

真机臂需事先安装 `elite_cs_sdk` wheel。
