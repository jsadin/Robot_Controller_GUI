#!/usr/bin/env python
"""一键设备连通检查（真机；失败打印原因不抛到系统）。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> int:
    from devices.config_loader import load_devices_config
    from devices.chassis import HermesClient, HermesError
    from devices.arm import ArmController
    from devices.camera import build_camera

    cfg = load_devices_config()
    print("config:", cfg.chassis.host, cfg.arm.host, cfg.camera.host)
    print("pc.local_ip:", cfg.pc_local_ip)

    # chassis
    try:
        info = HermesClient(cfg.chassis.host, cfg.chassis.port, timeout=3.0).ping()
        print("[OK] chassis", info.get("modelName"))
    except HermesError as e:
        print("[FAIL] chassis", e)

    # arm (mock or elite)
    arm = ArmController(cfg)
    ok = arm.connect()
    print("[OK] arm" if ok else "[FAIL] arm", arm.last_connect_error() or cfg.arm.kind)
    arm.disconnect()

    # camera
    cam = build_camera(cfg)
    if cfg.camera.kind == "mock" or cam.open():
        frame = cam.read_bgr() if cfg.camera.kind != "mock" else None
        if cfg.camera.kind == "mock":
            from devices.camera import MockCameraBackend
            m = MockCameraBackend()
            m.open()
            frame = m.read_bgr()
            m.close()
            print("[OK] camera mock frame", None if frame is None else frame.shape)
        else:
            print("[OK] camera" if frame is not None else "[WARN] camera open but no frame yet")
            cam.close()
    else:
        print("[FAIL] camera open")

    # elite sdk presence
    try:
        import elite_cs_sdk  # noqa: F401

        print("[OK] elite_cs_sdk import")
    except ImportError:
        print("[WARN] elite_cs_sdk not installed — use arm.kind: mock")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
