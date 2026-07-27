"""Phase0 冒烟：配置 / Mock 臂 / EStop / 相机 Mock；真机可选跳过。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class TestConfigAndDevices(unittest.TestCase):
    def test_load_config(self):
        from devices.config_loader import load_devices_config

        cfg = load_devices_config()
        self.assertEqual(cfg.chassis.host, "192.168.11.1")
        self.assertEqual(cfg.arm.host, "192.168.11.150")
        self.assertEqual(cfg.camera.host, "192.168.11.101")
        self.assertTrue(cfg.pc_local_ip)

    def test_mock_arm_and_estop(self):
        from devices.arm import ArmController
        from devices.common import EStopBus
        from devices.config_loader import load_devices_config

        cfg = load_devices_config()
        cfg.arm.kind = "mock"
        arm = ArmController(cfg)
        self.assertTrue(arm.connect())
        arm.sync_joint_desired_deg([10, 0, 0, 0, 0, 0])
        self.assertTrue(arm.advance_joint_command())
        j = arm.read_joints_deg()
        self.assertIsNotNone(j)

        bus = EStopBus()
        bus.bind(arm=arm)
        bus.trigger()
        self.assertTrue(bus.latched)
        self.assertTrue(arm.motion_halted)
        bus.release()
        self.assertFalse(bus.latched)

    def test_mock_camera_frame(self):
        from devices.camera import MockCameraBackend

        cam = MockCameraBackend()
        self.assertTrue(cam.open())
        frame = cam.read_bgr()
        self.assertIsNotNone(frame)
        self.assertEqual(frame.shape[2], 3)
        cam.close()

    def test_ranging_stub(self):
        from devices.config_loader import load_devices_config
        from devices.ranging import build_ranging

        r = build_ranging(load_devices_config())
        self.assertIsNone(r.get_distance_m())

    def test_mission_store(self):
        from core.mission import Mission, MissionStatus, MissionStep, MissionStore

        with tempfile.TemporaryDirectory() as td:
            store = MissionStore(Path(td) / "m.db")
            m = Mission(
                None,
                "t1",
                [MissionStep("wait", {"seconds": 0.1})],
                MissionStatus.PENDING,
            )
            store.save(m)
            self.assertIsNotNone(m.id)
            listed = store.list_missions()
            self.assertEqual(len(listed), 1)


class TestChassisOptional(unittest.TestCase):
    """真机在线时设置 ROBOT_SMOKE_CHASSIS=1。"""

    def test_chassis_ping(self):
        import os

        if os.environ.get("ROBOT_SMOKE_CHASSIS") != "1":
            self.skipTest("set ROBOT_SMOKE_CHASSIS=1 for live chassis")
        from devices.chassis import HermesClient
        from devices.config_loader import load_devices_config

        cfg = load_devices_config()
        c = HermesClient(cfg.chassis.host, cfg.chassis.port, timeout=3.0)
        info = c.ping()
        self.assertIn("modelName", info or {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
