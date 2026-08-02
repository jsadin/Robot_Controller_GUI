"""Elite CS Python SDK adapter (optional dependency: ``elite_cs_sdk``)."""

from __future__ import annotations

import importlib
import socket
import sys
import time
from types import ModuleType

from devices.arm.elite_config import EliteBackendConfig
from devices.arm.types import CartesianTarget, JointState6


def _load_sdk() -> ModuleType:
    try:
        return importlib.import_module("elite_cs_sdk")
    except ImportError as e:
        raise ImportError(
            "elite_cs_sdk is not installed. Build/install the vendor wheel from "
            "Elite_Robots_CS_SDK_Python, then re-run."
        ) from e


def _exc_message(exc: BaseException) -> str:
    return str(exc).strip() or exc.__class__.__name__


def _guess_local_ip_for_robot(robot_ip: str) -> str:
    """Pick the NIC used to reach the robot (VMware host-only friendly)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(0.5)
            s.connect((robot_ip, 30001))
            ip = s.getsockname()[0]
        if ip and not ip.startswith("127."):
            return ip
    except OSError:
        pass
    return ""


class EliteCsRobotBackend:
    """
    Thin Elite driver wrapper.

    Prerequisites (per vendor docs): robot External Control task running and
    reachable; optional RTSI recipe files for ``read_joints_rad``.
    Power-on / brake / play are intentionally out of scope here — use the
    teach pendant or your own bootstrap script.
    """

    def __init__(self, cfg: EliteBackendConfig) -> None:
        self._cfg = cfg
        self._cs = _load_sdk()
        self._driver = None
        self._rtsi = None
        self._last_cmd = JointState6((0.0, 0.0, 0.0, 0.0, 0.0, 0.0))
        self._last_cart_pose: CartesianTarget | None = None
        self._last_connect_error: str | None = None

    @property
    def last_connect_error(self) -> str | None:
        return self._last_connect_error

    def _fail_connect(self, message: str) -> bool:
        self._last_connect_error = message
        self.close()
        return False

    def _attach_persistent_rtsi(self) -> None:
        self._rtsi = None
        if self._cfg.skip_rtsi:
            return
        if not (self._cfg.rtsi_output_recipe and self._cfg.rtsi_input_recipe):
            return
        try:
            self._rtsi = self._cs.RtsiIOInterface(
                self._cfg.rtsi_output_recipe,
                self._cfg.rtsi_input_recipe,
                float(self._cfg.rtsi_frequency_hz),
            )
            if not self._rtsi.connect(self._cfg.robot_ip):
                self._rtsi = None
                print(
                    "[elite_teleop_gui] RTSI connect failed; joint readback disabled.",
                    file=sys.stderr,
                )
        except Exception as exc:
            self._rtsi = None
            print(
                f"[elite_teleop_gui] RTSI connect skipped: {_exc_message(exc)}",
                file=sys.stderr,
            )

    def _rtsi_snapshot_once(self) -> JointState6 | None:
        """Connect RTSI briefly to read actual joints (used with --skip-rtsi)."""
        if not (self._cfg.rtsi_output_recipe and self._cfg.rtsi_input_recipe):
            return None
        try:
            r = self._cs.RtsiIOInterface(
                self._cfg.rtsi_output_recipe,
                self._cfg.rtsi_input_recipe,
                float(self._cfg.rtsi_frequency_hz),
            )
            if not r.connect(self._cfg.robot_ip):
                return None
            try:
                j = r.getActualJointPositions()
            finally:
                try:
                    r.disconnect()
                except Exception:
                    pass
            if j is not None and len(j) >= 6:
                return JointState6(tuple(float(x) for x in j[:6]))
        except Exception as exc:
            print(
                f"[elite_teleop_gui] One-shot RTSI snapshot failed: {_exc_message(exc)}",
                file=sys.stderr,
            )
        return None

    def _seed_initial_pose_and_first_hold(self) -> None:
        """
        Resolve actual joint pose and send an immediate hold servoj (timeout_ms=0).

        If the PC waits for the Qt timer before the first reverse_socket packet, the
        controller times out (~100ms after the previous packet). Invalid servoj
        (velocity limit) causes the same symptom because the robot ignores the command.
        """
        assert self._driver is not None
        have_snapshot = False
        recipes_ok = bool(self._cfg.rtsi_output_recipe and self._cfg.rtsi_input_recipe)

        if recipes_ok and not self._cfg.skip_rtsi:
            self._attach_persistent_rtsi()
            if self._rtsi is not None:
                try:
                    j = self._rtsi.getActualJointPositions()
                    if j is not None and len(j) >= 6:
                        self._last_cmd = JointState6(tuple(float(x) for x in j[:6]))
                        have_snapshot = True
                except Exception as exc:
                    print(
                        f"[elite_teleop_gui] RTSI getActualJointPositions failed: {_exc_message(exc)}",
                        file=sys.stderr,
                    )
        elif recipes_ok and self._cfg.skip_rtsi and self._cfg.rtsi_snapshot_on_connect:
            snap = self._rtsi_snapshot_once()
            if snap is not None:
                self._last_cmd = snap
                have_snapshot = True

        boot = self._cfg.bootstrap_joints_deg
        if not have_snapshot and boot is not None and len(boot) == 6:
            self._last_cmd = JointState6.from_degrees(boot)
            have_snapshot = True

        if not have_snapshot:
            print(
                "[elite_teleop_gui] WARN: No RTSI/bootstrap joint snapshot; assuming 0 rad. "
                "If the simulator home differs, use --bootstrap-joints-deg or enable RTSI.",
                file=sys.stderr,
            )
            self._last_cmd = JointState6((0.0, 0.0, 0.0, 0.0, 0.0, 0.0))

        hold_ms = int(self._cfg.servoj_hold_timeout_ms)
        try:
            ok = bool(self._driver.writeServoj(list(self._last_cmd.q), hold_ms, False))
            if not ok:
                print(
                    "[elite_teleop_gui] WARN: Initial hold writeServoj returned false.",
                    file=sys.stderr,
                )
        except Exception as exc:
            print(f"[elite_teleop_gui] WARN: Initial hold failed: {_exc_message(exc)}", file=sys.stderr)

    def command_baseline_rad(self) -> JointState6:
        """Baseline pose after connect / last successful command (for speed limiter)."""
        return self._last_cmd

    def last_commanded_deg6(self) -> tuple[float, float, float, float, float, float]:
        """Sliders seed when RTSI readback is unavailable (bootstrap / hold pose)."""
        return self._last_cmd.as_degrees()

    def _build_driver_config(self):
        dc = self._cs.EliteDriverConfig()
        dc.robot_ip = self._cfg.robot_ip
        local_ip = self._cfg.local_ip.strip()
        if not local_ip:
            local_ip = _guess_local_ip_for_robot(self._cfg.robot_ip)
        dc.local_ip = local_ip
        dc.servoj_time = float(self._cfg.servoj_time)
        dc.servoj_gain = int(self._cfg.servoj_gain)
        dc.servoj_lookahead_time = float(self._cfg.servoj_lookahead_time)
        sf = self._cfg.script_file_path.strip()
        if not sf:
            pkg = sys.modules.get("elite_cs_sdk")
            if pkg and getattr(pkg, "__file__", None):
                import os

                root = os.path.dirname(os.path.abspath(pkg.__file__))
                sf = os.path.join(root, "external_control.script")
        dc.script_file_path = sf
        dc.headless_mode = bool(self._cfg.headless_mode)
        return dc

    def connect(self) -> bool:
        self._last_connect_error = None
        self.close()
        settle = max(0.0, float(self._cfg.reconnect_settle_s))
        if settle > 0:
            time.sleep(settle)

        dc = self._build_driver_config()
        retries = max(1, int(self._cfg.connect_retries))
        last_exc: BaseException | None = None

        for attempt in range(retries):
            if attempt > 0:
                self.close()
                time.sleep(settle * attempt)
            try:
                self._driver = self._cs.EliteDriver(dc)
                last_exc = None
                break
            except Exception as exc:
                last_exc = exc

        if last_exc is not None:
            hint = (
                f"无法连接机器人主端口 30001（{self._cfg.robot_ip}）：{_exc_message(last_exc)}\n\n"
                "请检查：\n"
                "1) 虚拟机控制器/ELISIM 已启动，示教器可 ping 通 Windows；\n"
                "2) Windows 能 ping 192.168.137.128；\n"
                "3) 断开后等待数秒再连，或重启仿真；\n"
                "4) 指定本机网卡：--local-ip <Windows 在 137 网段的 IP>；\n"
                "5) 若仅 RTSI 报「网络重名」，可加 --skip-rtsi 先测 servoj。"
            )
            return self._fail_connect(hint)

        deadline = time.monotonic() + float(self._cfg.connect_timeout_s)
        if dc.headless_mode:
            try:
                if not self._driver.isRobotConnected():
                    if not self._driver.sendExternalControlScript():
                        return self._fail_connect(
                            "headless 模式下无法向机器人下发 external_control 脚本。"
                        )
            except Exception as exc:
                return self._fail_connect(f"headless 脚本下发失败：{_exc_message(exc)}")

        try:
            while not self._driver.isRobotConnected() and time.monotonic() < deadline:
                time.sleep(0.01)
            if not self._driver.isRobotConnected():
                return self._fail_connect(
                    f"连接超时（{self._cfg.connect_timeout_s:.0f}s）：主端口 30001 已通，但控制器未在超时内连上本机反向端口。\n"
                    f"SDK 在本机监听：50001（reverse）、50002（script sender）、50003（trajectory）、50004（script command）；"
                    f"脚本里应能访问 local_ip={dc.local_ip or '(自动)'}。\n\n"
                    "常见原因：\n"
                    "1) Windows 防火墙或其它安全软件拦截「入站」TCP 50001–50004（网络助手只测了出站连 30001，无法覆盖此项）。\n"
                    "   处理：在「高级安全 Windows Defender 防火墙」中为专用网络添加入站规则，允许 TCP 50001–50004，"
                    "或暂时关闭防火墙试连以确认。\n"
                    "2) 示教器未运行 / 未播放 external control 程序（headless 下发失败或脚本未执行 socket_open）。\n"
                    "3) local_ip 不是控制器路由到的那张网卡地址（虚拟机/多网卡时常见）。\n"
                    "4) 可延长等待：命令行加 --connect-timeout 90 再试。"
                )
        except Exception as exc:
            return self._fail_connect(f"等待机器人连接时出错：{_exc_message(exc)}")

        self._seed_initial_pose_and_first_hold()
        return True

    def close(self) -> None:
        if self._rtsi is not None:
            try:
                self._rtsi.disconnect()
            except Exception:
                pass
            self._rtsi = None
        if self._driver is not None:
            try:
                self._driver.stopControl()
            except Exception:
                pass
            self._driver = None

    def is_connected(self) -> bool:
        if self._driver is None:
            return False
        try:
            return bool(self._driver.isRobotConnected())
        except Exception:
            return False

    def read_joints_rad(self) -> JointState6 | None:
        if self._rtsi is None:
            return None
        try:
            j = self._rtsi.getActualJointPositions()
        except Exception:
            return None
        if j is None or len(j) < 6:
            return None
        return JointState6(tuple(float(x) for x in j[:6]))

    def emergency_stop(self) -> None:
        """进入 idle，停止接受持续 servoj（软急停）。"""
        if self._driver is None:
            return
        try:
            self._driver.writeIdle(int(self._cfg.idle_command_timeout_ms))
        except Exception:
            pass

    def clear_emergency_stop(self) -> None:
        """退出 idle：按下发 hold servoj 重新接管外部控制。

        ``writeIdle`` 后若不恢复 servoj 流，控制器会一直停在 idle，
        软件闩锁虽已解除但臂表现为「连着却不动」。
        """
        if self._driver is None:
            return
        try:
            if not self.is_connected():
                return
        except Exception:
            return
        pose = self.read_joints_rad()
        if pose is None:
            pose = self._last_cmd
        if pose is None:
            return
        self._last_cmd = pose
        hold_ms = int(self._cfg.servoj_hold_timeout_ms)
        pos = list(pose.q)
        try:
            ok = bool(self._driver.writeServoj(pos, hold_ms, False))
            if not ok:
                ms = int(self._cfg.servoj_timeout_ms)
                ok = bool(self._driver.writeServoj(pos, ms, False))
            if not ok:
                print(
                    "[elite_teleop_gui] WARN: clear_emergency_stop writeServoj returned false.",
                    file=sys.stderr,
                )
        except Exception as exc:
            print(
                f"[elite_teleop_gui] WARN: clear_emergency_stop failed: {_exc_message(exc)}",
                file=sys.stderr,
            )

    def command_joints_rad(self, joints: JointState6, timeout_ms: int | None = None) -> bool:
        if self._driver is None:
            return False
        if timeout_ms is None:
            ms = int(self._cfg.servoj_timeout_ms)
        else:
            ms = int(timeout_ms)
        if not self.is_connected():
            return False
        pos = list(joints.q)
        try:
            ok = bool(self._driver.writeServoj(pos, ms, False))
        except Exception:
            return False
        if ok:
            self._last_cmd = joints
        return ok

    def command_cartesian(self, pose: CartesianTarget, timeout_ms: int | None = None) -> bool:
        if self._driver is None or not self.is_connected():
            return False
        ms = int(timeout_ms if timeout_ms is not None else self._cfg.servoj_timeout_ms)
        pos = pose.as_pose_list()
        try:
            ok = bool(self._driver.writeServoj(pos, ms, True))
        except Exception:
            return False
        if ok:
            self._last_cart_pose = pose
        return ok

    def brake_release(self) -> bool:
        """预留：Dashboard brakeRelease（v1 未强制接通）。"""
        return False

    def start_freedrive(self) -> bool:
        return False

    def stop_freedrive(self) -> bool:
        return False
