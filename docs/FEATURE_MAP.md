# 功能明细表 ↔ 实现映射

状态说明：`done` 已有/本期必达 · `phaseN` 对应阶段 · `stub` 仅接口预留 · `later` v1 不做

## 总体

| # | 功能 | 状态 | 实现位置 |
|---|------|------|----------|
| 1 | 软件一键安装/双击进入 | phase4 | README + PyInstaller |
| 2 | 设备统一通信 | phase0–1 | `devices/` + `config/devices.yaml` |
| 3 | 任务组（底盘+臂+相机） | phase3 | `core/mission.py` |
| 4 | 主界面监测/全屏 | phase1–2 | `ui/main_window.py` |
| 5 | 急停整机 | phase0–1 | `devices/common/estop_bus.py` |
| 6 | 外部报警灯 | stub | `devices/common/alarm_light.py` |
| 7 | 诊断 | phase2 | `ui/diagnosis_panel.py` + `core/diagnosis.py`：运行态指标、约 2s 周期刷新、综合健康；`core/app_log.py` 落盘+环形缓冲，诊断页一键导出 zip |

## 底盘

| # | 功能 | 状态 | 备注 |
|---|------|------|------|
| 1–5,7–9 | 地图/墙/回桩/重定位/导航/雷达/跟随/星标/遥控 | done | 迁自 SLAM |
| 10 | 任务+巡检日历 | phase3 | 已并入 Mission（任务组） |
| 11 | 轨道 | stub | UI 隐藏 |
| 12 | 辅助定位端口 | stub | |

## 机械臂

| # | 功能 | 状态 |
|---|------|------|
| 1–3 | 关节调节/动作组/调速 | phase2 |
| 4 | 手动释放抱闸 | stub |
| 5 | 激光测距辅助定位 | stub |
| 6 | 防护雷达 | stub |

## 摄像头

| # | 功能 | 状态 |
|---|------|------|
| 1 | 动态视觉 | phase2 |
| 2 | 静态采集 | phase2 |
| 3–4 | 补光/焦距 | stub（无协议则灰显） |

## 数据分析

| 功能 | 状态 |
|------|------|
| 后期分析模块端口 | stub `core/analytics_port.py` |

## 命名规范（B6）

| 名称 | 含义 |
|------|------|
| **ChassisTask** | 已废弃 UI；能力并入 Mission（旧 tasks.db 启动时迁移） |
| **ArmActionSequence** | 机械臂动作组（位姿序列） |
| **Mission** | 跨设备任务组（串行步骤） |
