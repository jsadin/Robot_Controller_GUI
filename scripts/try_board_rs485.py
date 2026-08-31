"""一次性：连臂后读柜体 HF。密码只从 devices.local.yaml 读。"""
from __future__ import annotations

import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from devices.arm.controller import ArmController
from devices.config_loader import load_devices_config
from devices.ranging import hf_read_distance_frame, parse_modbus_fc04_distance


def main() -> int:
    cfg = load_devices_config()
    pw = (cfg.ranging.ssh_password or "").strip()
    if not pw:
        print("FAIL: ranging.ssh_password 为空")
        return 2
    print("arm.host", cfg.arm.host, "baud", cfg.ranging.baud, "ssh_len", len(pw))
    arm = ArmController(cfg)
    print("connecting arm...")
    ok = arm.connect()
    print("arm.connect", ok, "err", arm.last_connect_error())
    if not ok:
        return 4
    try:
        frame = bytes(hf_read_distance_frame(int(cfg.ranging.slave or 1)))
        print("tx", frame.hex())
        for i in range(3):
            raw = arm.read_cabinet_rs485(
                frame,
                read_n=9,
                timeout_ms=1000,
                ssh_password=pw,
                baud=int(cfg.ranging.baud or 115200),
                parity=int(cfg.ranging.parity or 0),
                tcp_port=int(cfg.ranging.rs485_tcp_port or 54322),
            )
            raw = bytes(raw or b"")
            print("rx", i, "len", len(raw), "hex", raw.hex() if raw else "")
            if len(raw) >= 7 and raw[3:7] == b"\x7f\xff\xff\xff":
                print("sensor invalid 0x7FFFFFFF")
                continue
            dist = parse_modbus_fc04_distance(raw)
            print("distance_mm", None if dist is None else round(dist * 1000.0, 1))
        return 0
    except Exception:
        traceback.print_exc()
        return 1
    finally:
        try:
            arm.disconnect()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
