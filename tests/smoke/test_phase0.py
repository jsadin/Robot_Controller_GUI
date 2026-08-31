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
        self.assertEqual(cfg.camera.host, "192.168.11.103")
        self.assertEqual(cfg.camera.http_port, 80)
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
        self.assertFalse(arm.advance_joint_command())
        bus.release()
        self.assertFalse(bus.latched)
        self.assertFalse(arm.motion_halted)
        arm.sync_joint_desired_deg([20, 0, 0, 0, 0, 0])
        self.assertTrue(arm.advance_joint_command())

    def test_mock_camera_frame(self):
        from devices.camera import MockCameraBackend

        cam = MockCameraBackend()
        self.assertTrue(cam.open())
        frame = cam.read_bgr()
        self.assertIsNotNone(frame)
        self.assertEqual(frame.shape[2], 3)
        jpeg = cam.snapshot_jpeg()
        self.assertIsNotNone(jpeg)
        self.assertGreater(len(jpeg), 32)
        self.assertEqual(jpeg[:2], b"\xff\xd8")
        from devices.camera import save_snapshot_bytes

        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "snap.jpg"
            save_snapshot_bytes(jpeg, out)
            self.assertTrue(out.is_file())
            self.assertGreater(out.stat().st_size, 32)
        cam.close()
        self.assertFalse(cam.ptz_available())

    def test_preview_stream_uses_sub(self):
        from devices.camera import _preview_stream_path

        self.assertEqual(
            _preview_stream_path("/h264/ch1/main/av_stream"),
            "/Streaming/Channels/102",
        )
        self.assertEqual(
            _preview_stream_path("/Streaming/Channels/101"),
            "/Streaming/Channels/102",
        )
        self.assertEqual(
            _preview_stream_path("/Streaming/Channels/102"),
            "/Streaming/Channels/102",
        )

    def test_hikvision_isapi_xml_and_gate(self):
        from devices.camera.hikvision_isapi import HikvisionIsapi, _ptz_xml
        from devices.config_loader import CameraCfg

        xml = _ptz_xml(zoom=50)
        self.assertIn("<zoom>50</zoom>", xml)
        self.assertNotIn("focus", xml)
        cfg = CameraCfg(kind="hikvision", host="192.168.11.101")
        self.assertTrue(HikvisionIsapi(cfg).available())
        cfg_usb = CameraCfg(kind="usb", host="192.168.11.101")
        self.assertFalse(HikvisionIsapi(cfg_usb).available())

    def test_auto_zoom_gears(self):
        from devices.camera.auto_zoom import AutoZoomController, want_max_zoom

        self.assertTrue(want_max_zoom(1.5, 1.0))
        self.assertFalse(want_max_zoom(1.0, 1.0))
        self.assertFalse(want_max_zoom(0.15, 1.0))
        ctl = AutoZoomController(near_m=1.0)
        ctl.set_enabled(True)
        self.assertEqual(ctl.tick(None), "none")
        self.assertEqual(ctl.tick(1.5), "start_max")
        self.assertEqual(ctl.tick(1.2), "hold_max")
        ctl.on_travel_done()
        self.assertEqual(ctl.tick(1.5), "none")
        self.assertEqual(ctl.tick(0.8), "start_min")
        self.assertEqual(ctl.tick(0.15), "hold_min")
        ctl.on_travel_done()
        self.assertEqual(ctl.tick(0.15), "none")
        self.assertEqual(ctl.tick(None), "none")
        self.assertEqual(ctl.tick(None, too_far=True), "start_max")
        ctl.set_enabled(False)
        self.assertEqual(ctl.tick(1.5), "none")

    def test_ranging_stub(self):
        from devices.config_loader import load_devices_config
        from devices.ranging import (
            build_ranging,
            distance_m_from_input_regs,
            parse_ranging_reply,
        )

        cfg = load_devices_config()
        cfg.ranging.enabled = False
        r = build_ranging(cfg)
        self.assertFalse(r.enabled)
        self.assertIsNone(r.get_distance_m())
        self.assertAlmostEqual(distance_m_from_input_regs(0, 1187), 1.187)
        self.assertAlmostEqual(distance_m_from_input_regs(0, 1750), 1.75)
        self.assertAlmostEqual(parse_ranging_reply("True;[0, 1187]"), 1.187)
        self.assertIsNone(parse_ranging_reply("True;[]"))
        self.assertIsNone(distance_m_from_input_regs(32767, 65535))
        self.assertIsNone(parse_ranging_reply("True;[32767, 65535]"))
        from devices.arm.cabinet_rs485 import _BRIDGE_PY, _stty_uart
        from devices.ranging import hf_read_distance_frame

        self.assertIn("termios.B115200", _BRIDGE_PY)
        self.assertIn("readlink -f", _stty_uart("/dev/ttyBoard", 115200))

        self.assertEqual(hf_read_distance_frame(1), [1, 4, 0, 0, 0, 2, 0x71, 0xCB])
        from devices.ranging import parse_modbus_fc04_distance

        self.assertIsNone(parse_modbus_fc04_distance(bytes.fromhex("0104047fffffffd3d0")))
        from devices.ranging import pack_mm_checksum, unpack_mm_checksum

        w = pack_mm_checksum(119)
        self.assertEqual(unpack_mm_checksum(w), 119)
        self.assertIsNone(unpack_mm_checksum(0xFFFFFFFF))
        self.assertIsNone(unpack_mm_checksum(0))
        self.assertIsNone(unpack_mm_checksum(pack_mm_checksum(65535)))
        self.assertIsNone(unpack_mm_checksum(0x0000FFFF))

        from devices.ranging import EliteCabinetRanging, _MAGIC, pack_mm_checksum

        backend = EliteCabinetRanging(cfg)

        class _BitsArm:
            def get_output_bit_registers_0_31(self):
                return pack_mm_checksum(1187)

            def get_output_int_register(self, i):
                return 0

            def get_analog_output(self, i):
                return 0.0

        backend._arm = _BitsArm()
        self.assertAlmostEqual(backend._from_arm_registers(), 1.187)

        class _IntArm:
            def get_output_int_register(self, i):
                return 119 if i == 0 else _MAGIC

            def get_analog_output(self, i):
                return None

        backend._arm = _IntArm()
        self.assertAlmostEqual(backend._from_arm_registers(), 0.119)

        class _AnalogArm:
            def get_output_int_register(self, i):
                return 0

            def get_analog_output(self, i):
                return 0.1187 if i == 0 else 0.73

        backend._arm = _AnalogArm()
        self.assertAlmostEqual(backend._from_arm_registers(), 1.187)

        class _DeadBitsArm:
            def get_output_bit_registers_0_31(self):
                return 0x7FFFFFFF

            def get_output_int_register(self, i):
                return 2147483647 if i == 0 else 0

            def get_analog_output(self, i):
                return 0.0

        backend._arm = _DeadBitsArm()
        self.assertIsNone(backend._from_arm_registers())

        class _MaxBitsArm:
            def get_output_bit_registers_0_31(self):
                return 0x0000FFFF

            def get_output_int_register(self, i):
                return 0

            def get_analog_output(self, i):
                return 0.0

        backend._arm = _MaxBitsArm()
        self.assertIsNone(backend._from_arm_registers())
        script = backend._sec_script()
        self.assertIn("write_output_boolean_register(32, True)", script)
        self.assertIn("(n % 2) != 0", script)
        self.assertNotIn("n // 2", script)
        self.assertNotIn("tenths &", script)
        from devices.ranging import parse_modbus_fc04_distance

        self.assertAlmostEqual(
            parse_modbus_fc04_distance(bytes([1, 4, 4, 0, 0, 4, 163])), 1.187
        )
        self.assertNotIn("n // 2", script)

    def test_inject_cabinet_ranging_thread(self):
        from devices.arm.elite_robot import inject_cabinet_ranging_thread

        src = (
            "def foo():\n"
            "    pass\n"
            "# HEADER_END\n"
            "script_command_thread_handle = start_thread(scriptCommands, ())\n"
            "move_thread_handle = 0\n"
            "stop_thread(script_command_thread_handle)\n"
            "join_thread(script_command_thread_handle)\n"
        )
        out = inject_cabinet_ranging_thread(src, baud=115200, parity=0, slave=1)
        self.assertIn("def rangingThread():", out)
        self.assertIn("write_output_boolean_register(32, bool(valid) and t > 0)", out)
        self.assertIn("def packRangeBits(tenths, valid):", out)
        self.assertIn("n = 65535 - t", out)
        self.assertIn("ranging_thread_handle = start_thread(rangingThread, ())", out)
        self.assertIn("stop_thread(ranging_thread_handle)", out)
        self.assertEqual(out, inject_cabinet_ranging_thread(out))

        sdk_script = (
            ROOT
            / "ES66"
            / "ELITE_ROBOTS_ES66"
            / "elite_teleop_gui"
            / ".venv"
            / "Lib"
            / "site-packages"
            / "elite_cs_sdk"
            / "external_control.script"
        )
        if sdk_script.is_file():
            raw = sdk_script.read_text(encoding="utf-8")
            patched = inject_cabinet_ranging_thread(raw, baud=115200, parity=0, slave=1)
            self.assertNotEqual(patched, raw)
            self.assertIn("def rangingThread():", patched)

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
