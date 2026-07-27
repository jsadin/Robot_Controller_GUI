#!/usr/bin/env python
"""真机三设备连通测试（底盘 / 机械臂 / 摄像头）。"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def section(title: str) -> None:
    print("\n" + "=" * 48)
    print(title)
    print("=" * 48)


def test_chassis(cfg) -> bool:
    from devices.chassis import HermesClient, HermesError

    section("1) 底盘 " + cfg.chassis.host)
    try:
        c = HermesClient(cfg.chassis.host, cfg.chassis.port, timeout=4.0)
        info = c.ping()
        pose = c.get_pose()
        power = c.get_power_status()
        print("[OK] model:", info.get("modelName"))
        print("[OK] pose: x=%.3f y=%.3f yaw=%.3f" % (pose.x, pose.y, pose.yaw))
        print("[OK] battery:", power.battery_percentage, "%")
        return True
    except HermesError as e:
        print("[FAIL]", e)
        return False


def test_arm(cfg) -> bool:
    from devices.arm import ArmController

    section("2) 机械臂 " + cfg.arm.host + " kind=" + cfg.arm.kind)
    print("pc.local_ip:", cfg.pc_local_ip)
    if cfg.arm.kind != "elite_cs":
        print("[WARN] arm.kind 不是 elite_cs，当前为", cfg.arm.kind)
    try:
        import elite_cs_sdk  # noqa: F401
        print("[OK] elite_cs_sdk 已安装")
    except ImportError:
        print("[FAIL] 未安装 elite_cs_sdk")
        print("       请先在 ES66 工程执行 scripts/build_elite_sdk_windows.ps1 并 pip install 生成的 wheel")
        return False

    arm = ArmController(cfg)
    ok = arm.connect()
    if not ok:
        print("[FAIL] 连接失败:", arm.last_connect_error())
        return False
    print("[OK] 已连接")
    j = arm.read_joints_deg()
    print("[OK] 关节角(deg):", None if j is None else tuple(round(x, 2) for x in j))
    arm.disconnect()
    print("[OK] 已断开")
    return True


def test_camera(cfg) -> bool:
    from devices.camera import build_camera

    section("3) 摄像头 " + cfg.camera.host)
    pw = cfg.camera.password or os.environ.get("ROBOT_CAMERA_PASSWORD", "")
    if not pw:
        print("[FAIL] 无密码：请在 config/devices.local.yaml 填写 camera.password")
        print("       或: $env:ROBOT_CAMERA_PASSWORD='你的密码'")
        return False
    print("[INFO] user:", cfg.camera.user, "password: ****")
    cam = build_camera(cfg)
    if not cam.open():
        print("[FAIL] open 失败（检查账号密码 / RTSP 路径 / 防火墙）")
        return False
    frame = None
    for _ in range(30):
        frame = cam.read_bgr()
        if frame is not None:
            break
        time.sleep(0.1)
    cam.close()
    if frame is None:
        print("[FAIL] 已 open 但未读到帧")
        return False
    print("[OK] 帧尺寸:", frame.shape)
    return True


def main() -> int:
    from devices.config_loader import load_devices_config

    cfg = load_devices_config()
    print("加载配置:", cfg.chassis.host, cfg.arm.host, cfg.camera.host)
    print("local_ip:", cfg.pc_local_ip, "arm.kind:", cfg.arm.kind)

    results = {
        "chassis": test_chassis(cfg),
        "arm": test_arm(cfg),
        "camera": test_camera(cfg),
    }
    section("汇总")
    for k, v in results.items():
        print(f"  {k}: {'PASS' if v else 'FAIL'}")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
