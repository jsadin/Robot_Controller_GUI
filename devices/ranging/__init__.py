"""激光测距：柜体 RS485（Elite masterboard MODBUS-RTU）或 stub。"""

from __future__ import annotations

import re
import socket
import threading
import time
from typing import Optional, Tuple

from devices.config_loader import DevicesConfig, RangingCfg

_REPLY_REGS = re.compile(r"\[(\d+)\s*,\s*(\d+)\]")


_MAX_MM = 20000
_INT_INVALID = 0x7FFFFFFF
_MAGIC = 0xA5A5
_ANALOG_MARK = 0.73


def plausible_mm(mm: int) -> bool:
    return 1 <= int(mm) <= _MAX_MM


def distance_m_from_input_regs(high: int, low: int) -> Optional[float]:
    """HF 输入寄存器：u32 高/低字，单位 1mm（现场对照：0.175 应对应 1750mm）。"""
    raw = ((int(high) & 0xFFFF) << 16) | (int(low) & 0xFFFF)
    if raw <= 0 or raw >= _INT_INVALID:
        return None
    mm = float(raw)
    if mm > _MAX_MM:
        return None
    return mm / 1000.0


def modbus_crc(data: bytes) -> bytes:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 0x0001:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return bytes([crc & 0xFF, (crc >> 8) & 0xFF])


def pack_mm_checksum(mm: int) -> int:
    """低 16 位=毫米，高 16 位=按位取反，用于识别 0xFFFFFFFF 这类死数。"""
    lo = int(mm) & 0xFFFF
    return lo | ((lo ^ 0xFFFF) << 16)


def unpack_mm_checksum(word: int) -> Optional[int]:
    w = int(word) & 0xFFFFFFFF
    lo = w & 0xFFFF
    hi = (w >> 16) & 0xFFFF
    if hi != (lo ^ 0xFFFF):
        return None
    if lo <= 0 or lo >= 0xFFFF:
        return None
    return lo


def tenths_to_distance_m(tenths: int) -> Optional[float]:
    """兼容旧名：寄存器/打包值按 1mm 计。"""
    t = int(tenths)
    if t <= 0 or t >= 0xFFFF:
        return None
    mm = float(t)
    if mm > _MAX_MM:
        return None
    return mm / 1000.0


def hf_read_distance_frame(slave: int) -> list[int]:
    """FC 0x04 读输入寄存器 0..1。"""
    req = bytes([int(slave) & 0xFF, 0x04, 0x00, 0x00, 0x00, 0x02])
    return list(req + modbus_crc(req))


def parse_ranging_reply(text: str) -> Optional[float]:
    """解析柜体脚本回传：``True;[0, 1187]``。"""
    raw = (text or "").strip()
    if ";" not in raw:
        return None
    ok, _, rest = raw.partition(";")
    if ok.strip().lower() != "true":
        return None
    rest = rest.strip()
    if not rest or rest == "[]":
        return None
    m = _REPLY_REGS.search(rest)
    if not m:
        return None
    return distance_m_from_input_regs(int(m.group(1)), int(m.group(2)))


def parse_modbus_fc04_distance(data: bytes) -> Optional[float]:
    """解析 HF FC04 应答：``01 04 04 HH HL LH LL …``。"""
    if not data or len(data) < 7:
        return None
    if int(data[1]) != 4 or int(data[2]) != 4:
        return None
    high = ((int(data[3]) & 0xFF) << 8) | (int(data[4]) & 0xFF)
    low = ((int(data[5]) & 0xFF) << 8) | (int(data[6]) & 0xFF)
    return distance_m_from_input_regs(high, low)


class RangingBackend:
    def __init__(self, cfg: RangingCfg) -> None:
        self._cfg = cfg

    @property
    def enabled(self) -> bool:
        return bool(self._cfg.enabled)

    @property
    def last_error(self) -> Optional[str]:
        return None

    def connect(self) -> bool:
        return False

    def close(self) -> None:
        return None

    def bind_arm(self, arm: object) -> None:
        return None

    def set_paused(self, paused: bool) -> None:
        return None

    def get_distance_m(self) -> Optional[float]:
        """未接入硬件时恒返回 None。"""
        return None


class EliteCabinetRanging(RangingBackend):
    """柜体 HF 测距。

    机械臂外部控制运行中：优先用柜体 SSH（``startBoardRs485``）在 PC 上直接读 485；
    未配置密码时才发 30001 ``sec``（不用 ``&``，以免脚本被丢弃）。
    未接臂时用 ``def`` + socket。
    """

    def __init__(self, devices: DevicesConfig) -> None:
        super().__init__(devices.ranging)
        self._devices = devices
        self._arm = None
        self._lock = threading.Lock()
        self._distance_m: Optional[float] = None
        self._error: Optional[str] = None
        self._stop = threading.Event()
        self._paused = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._arm_seen_mono: Optional[float] = None

    def bind_arm(self, arm: object) -> None:
        self._arm = arm

    @property
    def last_error(self) -> Optional[str]:
        with self._lock:
            return self._error

    def connect(self) -> bool:
        if self._use_ext_script_thread():
            return True
        self._ensure_worker()
        return True

    def close(self) -> None:
        self._stop.set()
        th = self._thread
        if th is not None and th.is_alive():
            th.join(timeout=2.0)
        self._thread = None

    def set_paused(self, paused: bool) -> None:
        if self._use_ext_script_thread():
            self._paused.clear()
            return
        if paused:
            self._paused.set()
        else:
            self._paused.clear()

    def get_distance_m(self) -> Optional[float]:
        self._ensure_worker()
        with self._lock:
            return self._distance_m

    def _arm_kind(self) -> str:
        cfg = getattr(self._arm, "config", None)
        arm_cfg = getattr(cfg, "arm", None)
        kind = str(getattr(arm_cfg, "kind", "") or "").strip().lower()
        if kind:
            return kind
        return str(getattr(self._devices.arm, "kind", "") or "").strip().lower()

    def _arm_connected(self) -> bool:
        fn = getattr(self._arm, "is_connected", None)
        if not callable(fn):
            return False
        try:
            return bool(fn())
        except Exception:
            return False

    def _use_ext_script_thread(self) -> bool:
        return False

    def _elite_arm_pending(self) -> bool:
        return self._arm_kind() in ("elite_cs", "elite", "elite_cs_sdk") and not self._arm_connected()

    def _from_arm_registers(self) -> Optional[float]:
        bits_fn = getattr(self._arm, "get_output_bit_registers_0_31", None)
        bool_fn = getattr(self._arm, "get_output_bool_register", None)
        int_fn = getattr(self._arm, "get_output_int_register", None)
        analog_fn = getattr(self._arm, "get_analog_output", None)
        bits_raw: Optional[int] = None
        hb40: Optional[bool] = None
        dist: Optional[float] = None
        if callable(bits_fn):
            try:
                bits_raw = bits_fn()
            except Exception:
                bits_raw = None
            if bits_raw is not None:
                tenths = unpack_mm_checksum(int(bits_raw))
                if tenths is not None:
                    dist = tenths_to_distance_m(tenths)
        if callable(bool_fn):
            try:
                hb40 = bool_fn(32)
            except Exception:
                hb40 = None
        if dist is None and callable(int_fn):
            try:
                val = int_fn(0)
                magic = int_fn(1)
            except Exception:
                val, magic = None, None
            if magic == _MAGIC and val is not None and plausible_mm(int(val)):
                dist = int(val) / 1000.0
        if dist is None and callable(analog_fn):
            try:
                a0 = analog_fn(0)
                a1 = analog_fn(1)
            except Exception:
                a0, a1 = None, None
            if a0 is not None and a1 is not None:
                x, y = float(a0), float(a1)
                if x > 1.5 or y > 1.5:
                    x, y = x / 10.0, y / 10.0
                if abs(y - _ANALOG_MARK) < 0.05:
                    cand = int(round(x * 10000.0))
                    if plausible_mm(cand):
                        dist = cand / 1000.0
        if dist is None:
            bits_s = (
                f"0x{int(bits_raw) & 0xFFFFFFFF:08X}" if bits_raw is not None else "none"
            )
            extra = f"(bits={bits_s} hb32={hb40})"
            with self._lock:
                self._error = "测距回读无效" + extra
            return None
        with self._lock:
            self._distance_m = dist
            self._error = None
        return dist

    def _ensure_worker(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="ranging-cabinet", daemon=True
        )
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            if self._paused.is_set():
                self._stop.wait(0.4)
                continue
            try:
                if self._elite_arm_pending():
                    self._arm_seen_mono = None
                    dist, err = None, "等待机械臂连接后并行测距"
                elif self._arm_connected():
                    now = time.monotonic()
                    if self._arm_seen_mono is None:
                        self._arm_seen_mono = now
                    # 等外部控制 hold servoj 站稳，再碰柜体 485，避免抢 30001/串口。
                    if now - self._arm_seen_mono < 2.0:
                        dist, err = None, "等待外部控制稳定后再测距"
                    elif (self._cfg.ssh_password or "").strip():
                        dist, err = self._read_via_board_rs485()
                    else:
                        dist, err = None, "已接臂：请配置 ranging.ssh_password，勿用 30001 sec"
                else:
                    self._arm_seen_mono = None
                    dist, err = self._read_once()
                with self._lock:
                    self._distance_m = dist
                    self._error = None if dist is not None else (err or "无应答")
            except Exception as e:
                with self._lock:
                    self._distance_m = None
                    self._error = str(e) or e.__class__.__name__
            self._stop.wait(2.5)

    def _arm_host(self) -> str:
        return (self._cfg.host or self._devices.arm.host or "").strip()

    def _listen_port(self) -> int:
        return int(self._cfg.port or 18770)

    def _send_30001(self, script: str) -> Optional[str]:
        arm = self._arm_host()
        if not arm:
            return "未配置 arm.host"
        try:
            with socket.create_connection((arm, 30001), timeout=3.0) as s:
                s.settimeout(1.0)
                try:
                    s.recv(256)
                except Exception:
                    pass
                s.sendall(script.encode("ascii"))
        except OSError as e:
            return f"30001 连接失败: {e}"
        return None

    def _sec_script(self) -> str:
        baud = int(self._cfg.baud or 115200)
        parity = int(self._cfg.parity or 0)
        slave = int(self._cfg.slave or 1)
        pack_lo = []
        pack_hi = []
        for i in range(16):
            pack_lo.append(f"    write_output_boolean_register({i}, (n % 2) != 0)")
            pack_lo.append("    n = (n - (n % 2)) / 2")
            pack_hi.append(f"    write_output_boolean_register({16 + i}, (n % 2) != 0)")
            pack_hi.append("    n = (n - (n % 2)) / 2")
        return (
            "sec ranging_sec():\n"
            "    write_output_boolean_register(32, True)\n"
            f"    masterboard_serial_config(True, {baud}, {parity}, 1, 8, True)\n"
            f"    d = masterboard_modbus_rtu_read_input_registers({slave}, 0, 2)\n"
            "    tenths = 0\n"
            "    if d:\n"
            "        if d[0] == 0:\n"
            "            if d[1] > 0:\n"
            "                if d[1] < 65535:\n"
            "                    tenths = d[1]\n"
            "    n = tenths\n"
            + "\n".join(pack_lo)
            + "\n    n = 65535 - tenths\n"
            + "\n".join(pack_hi)
            + "\nend\n"
        )

    def _read_via_board_rs485(self) -> Tuple[Optional[float], Optional[str]]:
        fn = getattr(self._arm, "read_cabinet_rs485", None)
        if not callable(fn):
            return None, "机械臂未提供柜体 485 读取"
        slave = int(self._cfg.slave or 1)
        frame = bytes(hf_read_distance_frame(slave))
        try:
            raw = fn(
                frame,
                read_n=9,
                timeout_ms=1000,
                ssh_password=str(self._cfg.ssh_password or ""),
                baud=int(self._cfg.baud or 115200),
                parity=int(self._cfg.parity or 0),
                tcp_port=int(self._cfg.rs485_tcp_port or 54322),
            )
        except Exception as e:
            return None, f"柜体 485 读取失败: {e}"
        if not raw:
            return None, "柜体 485 无数据（串口已通但测距仪无应答，检查波特率/接线）"
        raw_b = bytes(raw)
        if len(raw_b) >= 7 and raw_b[3:7] == b"\x7f\xff\xff\xff":
            return None, "测距仪已应答但无有效距离(0x7FFFFFFF)，请将目标放在量程内"
        dist = parse_modbus_fc04_distance(raw_b)
        if dist is None:
            return None, f"柜体 485 应答无法解析({raw_b[:12]!r})"
        return dist, None

    def _read_via_sec(self) -> Tuple[Optional[float], Optional[str]]:
        err = self._send_30001(self._sec_script())
        if err:
            return None, err
        deadline = time.monotonic() + 3.5
        last_detail = "次任务回读无有效距离"
        while time.monotonic() < deadline:
            dist = self._from_arm_registers()
            if dist is not None:
                return dist, None
            with self._lock:
                last_detail = self._error or last_detail
            time.sleep(0.2)
        return None, last_detail

    def _script(self, pc_ip: str, listen_port: int) -> str:
        slave = int(self._cfg.slave or 1)
        baud = int(self._cfg.baud or 115200)
        parity = int(self._cfg.parity or 0)
        return (
            "def ranging_rd():\n"
            f'    socket_open("{pc_ip}", {listen_port})\n'
            f"    ok = masterboard_serial_config(True, {baud}, {parity}, 1, 8, True)\n"
            "    sleep(0.15)\n"
            f"    d = masterboard_modbus_rtu_read_input_registers({slave}, 0, 2, 1.0)\n"
            '    socket_send_string(str(ok)+";"+str(d))\n'
            "    socket_close()\n"
            "end\n"
        )

    def _read_once(self) -> Tuple[Optional[float], Optional[str]]:
        arm = self._arm_host()
        pc = (self._devices.pc_local_ip or "").strip()
        if not arm or not pc:
            return None, "未配置 arm.host / pc.local_ip"
        listen_port = self._listen_port()
        got: list[bytes] = []

        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            srv.bind((pc, listen_port))
        except OSError:
            try:
                srv.bind(("0.0.0.0", listen_port))
            except OSError as e:
                srv.close()
                return None, f"回连端口 {listen_port} 绑定失败: {e}"
        srv.listen(1)
        srv.settimeout(4.0)

        def accept() -> None:
            try:
                conn, _addr = srv.accept()
                conn.settimeout(3.0)
                buf = b""
                try:
                    while True:
                        chunk = conn.recv(4096)
                        if not chunk:
                            break
                        buf += chunk
                except socket.timeout:
                    pass
                conn.close()
                got.append(buf)
            except Exception:
                pass

        th = threading.Thread(target=accept, daemon=True)
        th.start()
        time.sleep(0.05)
        try:
            with socket.create_connection((arm, 30001), timeout=3.0) as s:
                s.settimeout(1.0)
                try:
                    s.recv(256)
                except Exception:
                    pass
                s.sendall(self._script(pc, listen_port).encode("ascii"))
        except OSError as e:
            srv.close()
            return None, f"30001 连接失败: {e}"
        th.join(timeout=5.0)
        try:
            srv.close()
        except OSError:
            pass
        if not got:
            return None, "柜体未回连（检查本机防火墙入站）"
        dist = parse_ranging_reply(got[0].decode("utf-8", "replace"))
        if dist is None:
            return None, "柜体回连无有效距离"
        return dist, None


def build_ranging(cfg: DevicesConfig) -> RangingBackend:
    kind = (cfg.ranging.kind or "stub").strip().lower()
    if cfg.ranging.enabled and kind not in ("stub", "off", "none"):
        return EliteCabinetRanging(cfg)
    return RangingBackend(cfg.ranging)
