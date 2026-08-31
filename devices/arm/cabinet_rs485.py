"""柜体主板 485：Windows 上 SDK startBoardRs485 不可用时，SSH 起 Python 桥再 TCP 读写。"""

from __future__ import annotations

import base64
import os
import socket
import subprocess
import tempfile
import time
from pathlib import Path

_SSH_USERS = ("root", "elibot")

_BRIDGE_PY = r"""
import os, select, socket, sys
PORT = int(sys.argv[1])
DEV = sys.argv[2]
fd = os.open(DEV, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(('0.0.0.0', PORT))
srv.listen(4)
srv.settimeout(1.0)
while True:
    try:
        conn, _addr = srv.accept()
    except socket.timeout:
        continue
    conn.settimeout(0.2)
    try:
        while True:
            r, _, _ = select.select([conn, fd], [], [], 1.0)
            if conn in r:
                data = conn.recv(256)
                if not data:
                    break
                os.write(fd, data)
            if fd in r:
                try:
                    chunk = os.read(fd, 256)
                except BlockingIOError:
                    chunk = b''
                if chunk:
                    conn.sendall(chunk)
    except Exception:
        pass
    try:
        conn.close()
    except Exception:
        pass
"""


def _ssh_exe() -> str:
    p = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "OpenSSH" / "ssh.exe"
    return str(p) if p.is_file() else "ssh"


def ssh_run(host: str, password: str, user: str, command: str, timeout: float = 20.0) -> tuple[int, str, str]:
    ask = None
    try:
        fd, ask_path = tempfile.mkstemp(prefix="askpass_", suffix=".cmd")
        os.close(fd)
        ask = Path(ask_path)
        ask.write_text(f"@echo off\r\necho {password}\r\n", encoding="ascii")
        env = os.environ.copy()
        env["SSH_ASKPASS"] = str(ask)
        env["SSH_ASKPASS_REQUIRE"] = "force"
        env["DISPLAY"] = env.get("DISPLAY") or "localhost:0"
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        r = subprocess.run(
            [
                _ssh_exe(),
                "-o",
                "StrictHostKeyChecking=accept-new",
                "-o",
                "PreferredAuthentications=password",
                "-o",
                "PubkeyAuthentication=no",
                "-o",
                "KbdInteractiveAuthentication=no",
                "-o",
                "NumberOfPasswordPrompts=1",
                "-o",
                "ConnectTimeout=8",
                f"{user}@{host}",
                command,
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            stdin=subprocess.DEVNULL,
            startupinfo=startup,
        )
        return int(r.returncode), r.stdout or "", r.stderr or ""
    except Exception as exc:
        return 98, "", f"{exc.__class__.__name__}:{exc}"
    finally:
        if ask is not None:
            try:
                ask.unlink()
            except OSError:
                pass


def ssh_login_user(host: str, password: str) -> str | None:
    for user in _SSH_USERS:
        code, _out, _err = ssh_run(host, password, user, "id -u")
        if code == 0:
            return user
    return None


def send_board_serial_config(host: str, baud: int, parity: int) -> str | None:
    script = (
        "sec board_rs485_config():\n"
        f"    masterboard_serial_config(True, {int(baud)}, {int(parity)}, 1, 8, True)\n"
        "end\n"
    )
    try:
        with socket.create_connection((host, 30001), timeout=3.0) as s:
            s.settimeout(1.0)
            try:
                s.recv(256)
            except Exception:
                pass
            s.sendall(script.encode("ascii"))
    except OSError as e:
        return str(e)
    return None


def tcp_open(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def ensure_python_bridge(
    host: str,
    password: str,
    *,
    tcp_port: int = 54322,
    baud: int = 115200,
    parity: int = 0,
    device: str = "/dev/ttyBoard",
) -> str | None:
    """柜体无 socat：用自带 python3 把 tty 映射到 TCP。失败返回错误串。"""
    if tcp_open(host, tcp_port, 0.8):
        return None
    user = ssh_login_user(host, password)
    if not user:
        return "SSH 登录失败（用户 root/elibot）"
    # 外部控制运行中禁止往 30001 发 sec：失败会停掉主任务，机械臂随即不动。
    b64 = base64.b64encode(_BRIDGE_PY.encode("ascii")).decode("ascii")
    start = (
        "if [ -f /tmp/board_rs485_bridge.pid ]; then "
        "kill $(cat /tmp/board_rs485_bridge.pid) >/dev/null 2>&1 || true; fi; "
        f"python3 -c \"open('/tmp/board_rs485_bridge.py','w').write(__import__('base64').b64decode('{b64}').decode())\"; "
        f"nohup python3 /tmp/board_rs485_bridge.py {int(tcp_port)} {device} "
        f">/tmp/board_rs485_bridge.log 2>&1 & echo $! >/tmp/board_rs485_bridge.pid; "
        "echo PID:$(cat /tmp/board_rs485_bridge.pid); sleep 0.4"
    )
    code, out, err = ssh_run(host, password, user, start, timeout=15.0)
    if code != 0:
        return f"启动 485 桥失败 code={code} {(err or out).strip()[:200]}"
    deadline = time.monotonic() + 4.0
    while time.monotonic() < deadline:
        if tcp_open(host, tcp_port, 0.8):
            return None
        time.sleep(0.2)
    tail = ""
    _c, log, _e = ssh_run(host, password, user, "tail -n 30 /tmp/board_rs485_bridge.log 2>/dev/null")
    if log.strip():
        tail = log.strip()[:300]
    return f"485 桥未监听 {tcp_port} PID声明={(out or '').strip()[:80]} log={tail}"


def xfer(
    host: str,
    payload: bytes,
    *,
    tcp_port: int = 54322,
    read_n: int = 9,
    timeout_ms: int = 800,
) -> bytes:
    timeout_s = max(0.2, int(timeout_ms) / 1000.0)
    with socket.create_connection((host, int(tcp_port)), timeout=timeout_s) as s:
        s.settimeout(timeout_s)
        s.sendall(payload)
        buf = b""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline and len(buf) < int(read_n):
            try:
                chunk = s.recv(64)
            except socket.timeout:
                break
            if not chunk:
                break
            buf += chunk
        return buf
