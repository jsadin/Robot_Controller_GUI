"""ArmActionSequence 持久化（JSON）。"""

from __future__ import annotations

import json
import os
from pathlib import Path

FILE_VERSION = 1
MIN_STEP_DELAY_AFTER_S = 5.0


def default_sequences_path(data_dir: Path) -> Path:
    return data_dir / "arm_sequences.json"


def default_poses_path(data_dir: Path) -> Path:
    return data_dir / "arm_poses.json"


def load_sequences(path: Path) -> dict[str, tuple[bool, list[tuple[str, float]]]]:
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    entries = raw.get("sequences") if isinstance(raw, dict) else None
    if not isinstance(entries, list):
        return {}
    out: dict[str, tuple[bool, list[tuple[str, float]]]] = {}
    for row in entries:
        if not isinstance(row, dict):
            continue
        name = row.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        steps_raw = row.get("steps") or []
        steps: list[tuple[str, float]] = []
        if isinstance(steps_raw, list):
            for s in steps_raw:
                if not isinstance(s, dict):
                    continue
                pn = s.get("pose_name")
                if not isinstance(pn, str) or not pn.strip():
                    continue
                try:
                    d = float(s.get("delay_after_s", MIN_STEP_DELAY_AFTER_S))
                except (TypeError, ValueError):
                    d = MIN_STEP_DELAY_AFTER_S
                steps.append((pn.strip(), max(d, MIN_STEP_DELAY_AFTER_S)))
        out[name.strip()] = (bool(row.get("loop", False)), steps)
    return out


def save_sequences(path: Path, sequences: dict[str, tuple[bool, list[tuple[str, float]]]]) -> None:
    rows = []
    for name in sorted(sequences.keys()):
        loop, steps = sequences[name]
        rows.append(
            {
                "name": name,
                "loop": bool(loop),
                "steps": [{"pose_name": p, "delay_after_s": float(d)} for p, d in steps],
            }
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps({"version": FILE_VERSION, "sequences": rows}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    os.replace(tmp, path)


def load_poses(path: Path) -> dict[str, list[float]]:
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    poses = raw.get("poses") if isinstance(raw, dict) else None
    if not isinstance(poses, list):
        # also accept flat {name: [deg...]}
        if isinstance(raw, dict):
            out = {}
            for k, v in raw.items():
                if k == "version":
                    continue
                if isinstance(v, list) and len(v) == 6:
                    out[str(k)] = [float(x) for x in v]
            return out
        return {}
    out: dict[str, list[float]] = {}
    for row in poses:
        if not isinstance(row, dict):
            continue
        name = row.get("name")
        joints = row.get("joints_deg") or row.get("joints")
        if isinstance(name, str) and isinstance(joints, list) and len(joints) == 6:
            out[name.strip()] = [float(x) for x in joints]
    return out


def save_poses(path: Path, poses: dict[str, list[float]]) -> None:
    rows = [{"name": n, "joints_deg": poses[n]} for n in sorted(poses.keys())]
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps({"version": FILE_VERSION, "poses": rows}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    os.replace(tmp, path)
