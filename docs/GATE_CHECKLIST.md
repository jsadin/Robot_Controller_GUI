# Phase0 门禁清单（B1–B9）

| ID | 项 | 验证方式 | 状态 |
|----|----|----------|------|
| B1 | 统一 PyQt5 | 代码无 PyQt6 import（整合包） | 通过 |
| B2 | devices.yaml | `test_load_config` | 通过 |
| B3 | EStopBus | `test_mock_arm_and_estop` | 通过 |
| B4 | 臂控制线程 | `ui/arm_worker.py` | 通过（设计+代码） |
| B5 | pc.local_ip | 配置契约 + check 脚本打印 | 通过 |
| B6 | 命名规范 | FEATURE_MAP / INTERFACE_SPEC | 通过 |
| B7 | 数据目录 ~/.robot_controller | TaskStore / MissionStore | 通过 |
| B8 | elite_cs_sdk | Mock 绿；真机见 check 脚本 WARN | Mock 通过 |
| B9 | 摄像头配置 101 | devices.yaml + Mock 帧 | Mock 通过 |

真机勾选：`ROBOT_SMOKE_CHASSIS=1 python -m unittest ...` 与现场相机/臂联调。
