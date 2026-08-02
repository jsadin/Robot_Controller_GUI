"""现场配置包：可编辑目录 + ZIP 导出/加载（可扩展 module）。

目录（相对 pack 根，默认 {app_dir}/config）：
  pack.json
  devices.yaml / devices.local.yaml / rtsi/
  data/missions.db, arm_poses.json, arm_sequences.json
  chassis/walls.json, tracks.json

新增配置项：实现 PackModule 并登记到 KNOWN_MODULES / pack.json 即可。
未知 module 在加载时忽略，不抛错。
"""

from __future__ import annotations

import json
import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Protocol

from devices.config_loader import _app_dir, _bundle_root

SCHEMA_VERSION = 1
DEFAULT_MODULES = (
    "devices",
    "arm_data",
    "missions",
    "chassis_walls",
    "chassis_tracks",
)


@dataclass
class PackResult:
    ok: bool = True
    messages: list[str] = field(default_factory=list)
    modules_done: list[str] = field(default_factory=list)
    modules_skipped: list[str] = field(default_factory=list)

    def note(self, msg: str) -> None:
        self.messages.append(msg)

    def fail(self, msg: str) -> None:
        self.ok = False
        self.messages.append(msg)


class PackModule(Protocol):
    module_id: str

    def collect(self, ctx: "PackContext", dest_pack: Path) -> None: ...
    def apply(self, ctx: "PackContext", src_pack: Path) -> None: ...


@dataclass
class PackContext:
    """运行时上下文：数据目录、底盘客户端、日志回调。"""

    pack_dir: Path
    data_dir: Path
    client: Any = None  # HermesClient | None
    log: Callable[[str], None] = field(default=lambda _m: None)

    def has_client(self) -> bool:
        return self.client is not None


def default_pack_dir() -> Path:
    return _app_dir() / "config"


def pack_data_dir(pack_dir: Optional[Path] = None) -> Path:
    return (pack_dir or default_pack_dir()) / "data"


def _read_manifest(pack_dir: Path) -> dict[str, Any]:
    path = pack_dir / "pack.json"
    if not path.is_file():
        return {
            "schema_version": SCHEMA_VERSION,
            "name": "robot_field_pack",
            "modules": list(DEFAULT_MODULES),
        }
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {
            "schema_version": SCHEMA_VERSION,
            "name": "robot_field_pack",
            "modules": list(DEFAULT_MODULES),
        }
    if not isinstance(raw, dict):
        raw = {}
    mods = raw.get("modules")
    if not isinstance(mods, list) or not mods:
        raw["modules"] = list(DEFAULT_MODULES)
    raw.setdefault("schema_version", SCHEMA_VERSION)
    return raw


def _write_manifest(pack_dir: Path, manifest: Optional[dict] = None) -> None:
    pack_dir.mkdir(parents=True, exist_ok=True)
    data = manifest or _read_manifest(pack_dir)
    data["schema_version"] = int(data.get("schema_version") or SCHEMA_VERSION)
    if not isinstance(data.get("modules"), list) or not data["modules"]:
        data["modules"] = list(DEFAULT_MODULES)
    (pack_dir / "pack.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _safe_copy(src: Path, dst: Path) -> bool:
    if not src.is_file():
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return True


def _copy_tree_fill(src: Path, dst: Path, *, overwrite: bool = False) -> None:
    """将 src 下文件拷到 dst；overwrite=False 时不覆盖已存在文件。"""
    if not src.is_dir():
        return
    for p in src.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(src)
        target = dst / rel
        if target.exists() and not overwrite:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, target)


def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(obj, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


# ---- modules ----


class DevicesModule:
    module_id = "devices"

    def collect(self, ctx: PackContext, dest_pack: Path) -> None:
        src = ctx.pack_dir
        for name in ("devices.yaml", "devices.local.yaml", "pack.json"):
            _safe_copy(src / name, dest_pack / name)
        rtsi = src / "rtsi"
        if rtsi.is_dir():
            _copy_tree_fill(rtsi, dest_pack / "rtsi", overwrite=True)
        else:
            bundled = _bundle_root() / "config" / "rtsi"
            _copy_tree_fill(bundled, dest_pack / "rtsi", overwrite=False)

    def apply(self, ctx: PackContext, src_pack: Path) -> None:
        # 文件已由 import 落盘到 pack_dir；此处仅确保 rtsi 齐全
        if (src_pack / "rtsi").is_dir():
            _copy_tree_fill(src_pack / "rtsi", ctx.pack_dir / "rtsi", overwrite=True)
        for name in ("devices.yaml", "devices.local.yaml"):
            if (src_pack / name).is_file():
                _safe_copy(src_pack / name, ctx.pack_dir / name)


class ArmDataModule:
    module_id = "arm_data"

    def collect(self, ctx: PackContext, dest_pack: Path) -> None:
        for name in ("arm_poses.json", "arm_sequences.json"):
            _safe_copy(ctx.data_dir / name, dest_pack / "data" / name)

    def apply(self, ctx: PackContext, src_pack: Path) -> None:
        for name in ("arm_poses.json", "arm_sequences.json"):
            src = src_pack / "data" / name
            if src.is_file():
                _safe_copy(src, ctx.data_dir / name)


class MissionsModule:
    module_id = "missions"

    def collect(self, ctx: PackContext, dest_pack: Path) -> None:
        _safe_copy(ctx.data_dir / "missions.db", dest_pack / "data" / "missions.db")

    def apply(self, ctx: PackContext, src_pack: Path) -> None:
        src = src_pack / "data" / "missions.db"
        if src.is_file():
            _safe_copy(src, ctx.data_dir / "missions.db")


class ChassisWallsModule:
    module_id = "chassis_walls"

    def collect(self, ctx: PackContext, dest_pack: Path) -> None:
        walls_path = dest_pack / "chassis" / "walls.json"
        if ctx.has_client():
            try:
                raw = ctx.client.list_walls() or []
                out = []
                for w in raw:
                    if not isinstance(w, dict):
                        continue
                    s, e = w.get("start") or {}, w.get("end") or {}
                    out.append(
                        {
                            "start": {
                                "x": float(s.get("x", 0)),
                                "y": float(s.get("y", 0)),
                            },
                            "end": {
                                "x": float(e.get("x", 0)),
                                "y": float(e.get("y", 0)),
                            },
                        }
                    )
                _write_json(
                    walls_path,
                    {"version": 1, "walls": out},
                )
                ctx.log(f"walls: exported {len(out)} from chassis")
                return
            except Exception as e:
                ctx.log(f"walls: chassis export failed ({e}), keep local file")
        # fallback: copy existing snapshot
        local = ctx.pack_dir / "chassis" / "walls.json"
        if local.is_file():
            _safe_copy(local, walls_path)

    def apply(self, ctx: PackContext, src_pack: Path) -> None:
        src = src_pack / "chassis" / "walls.json"
        if src.is_file():
            _safe_copy(src, ctx.pack_dir / "chassis" / "walls.json")
        path = ctx.pack_dir / "chassis" / "walls.json"
        if not path.is_file() or not ctx.has_client():
            if path.is_file() and not ctx.has_client():
                ctx.log("walls: saved to pack; connect chassis to sync")
            return
        try:
            data = _read_json(path)
            walls = data.get("walls") if isinstance(data, dict) else None
            if not isinstance(walls, list):
                ctx.log("walls: invalid JSON, skip sync")
                return
            existing = ctx.client.list_walls() or []
            for w in existing:
                wid = w.get("id") if isinstance(w, dict) else None
                if wid is None:
                    continue
                try:
                    ctx.client.delete_wall(wid)
                except Exception:
                    pass
            n = 0
            for w in walls:
                if not isinstance(w, dict):
                    continue
                s, e = w.get("start") or {}, w.get("end") or {}
                ctx.client.add_wall(
                    float(s.get("x", 0)),
                    float(s.get("y", 0)),
                    float(e.get("x", 0)),
                    float(e.get("y", 0)),
                )
                n += 1
            ctx.log(f"walls: synced {n} to chassis")
        except Exception as e:
            ctx.log(f"walls: sync failed: {e}")


def _route_points_from_segments(segments: list) -> list[tuple[float, float]]:
    """将有序线段还原为折线顶点。"""
    pts: list[tuple[float, float]] = []
    for seg in segments:
        if isinstance(seg, dict):
            s, e = seg.get("start") or {}, seg.get("end") or {}
            x1, y1 = float(s.get("x", 0)), float(s.get("y", 0))
            x2, y2 = float(e.get("x", 0)), float(e.get("y", 0))
        else:
            continue
        if not pts:
            pts.append((x1, y1))
        pts.append((x2, y2))
    return pts


class ChassisTracksModule:
    module_id = "chassis_tracks"

    def collect(self, ctx: PackContext, dest_pack: Path) -> None:
        tracks_path = dest_pack / "chassis" / "tracks.json"
        if ctx.has_client():
            try:
                from devices.chassis.client import HermesClient

                raw = ctx.client.list_tracks() or []
                routes = HermesClient.group_tracks_by_route(raw)
                out = []
                for r in routes:
                    segs = r.get("segments") or []
                    pts = _route_points_from_segments(segs)
                    if len(pts) < 2:
                        continue
                    out.append(
                        {
                            "route_id": str(r.get("route_id") or ""),
                            "name": str(r.get("name") or ""),
                            "points": [[p[0], p[1]] for p in pts],
                        }
                    )
                _write_json(
                    tracks_path,
                    {"version": 1, "routes": out},
                )
                ctx.log(f"tracks: exported {len(out)} routes from chassis")
                return
            except Exception as e:
                ctx.log(f"tracks: chassis export failed ({e}), keep local file")
        local = ctx.pack_dir / "chassis" / "tracks.json"
        if local.is_file():
            _safe_copy(local, tracks_path)

    def apply(self, ctx: PackContext, src_pack: Path) -> None:
        src = src_pack / "chassis" / "tracks.json"
        if src.is_file():
            _safe_copy(src, ctx.pack_dir / "chassis" / "tracks.json")
        path = ctx.pack_dir / "chassis" / "tracks.json"
        if not path.is_file() or not ctx.has_client():
            if path.is_file() and not ctx.has_client():
                ctx.log("tracks: saved to pack; connect chassis to sync")
            return
        try:
            from devices.chassis.client import HermesClient

            data = _read_json(path)
            routes = data.get("routes") if isinstance(data, dict) else None
            if not isinstance(routes, list):
                ctx.log("tracks: invalid JSON, skip sync")
                return
            # 删除现有线路
            existing = HermesClient.group_tracks_by_route(
                ctx.client.list_tracks() or []
            )
            for r in existing:
                ids = [
                    s.get("id")
                    for s in (r.get("segments") or [])
                    if isinstance(s, dict)
                ]
                try:
                    ctx.client.delete_track_route(ids)
                except Exception:
                    pass
            n = 0
            for r in routes:
                if not isinstance(r, dict):
                    continue
                pts_raw = r.get("points") or []
                pts = []
                for p in pts_raw:
                    if isinstance(p, (list, tuple)) and len(p) >= 2:
                        pts.append((float(p[0]), float(p[1])))
                if len(pts) < 2:
                    continue
                ctx.client.add_track(
                    pts,
                    name=str(r.get("name") or ""),
                    route_id=str(r.get("route_id") or "") or None,
                )
                n += 1
            ctx.log(f"tracks: synced {n} routes to chassis")
        except Exception as e:
            ctx.log(f"tracks: sync failed: {e}")


KNOWN_MODULES: dict[str, PackModule] = {
    m.module_id: m
    for m in (
        DevicesModule(),
        ArmDataModule(),
        MissionsModule(),
        ChassisWallsModule(),
        ChassisTracksModule(),
    )
}


class PackManager:
    def __init__(
        self,
        pack_dir: Optional[Path] = None,
        data_dir: Optional[Path] = None,
    ) -> None:
        self.pack_dir = Path(pack_dir or default_pack_dir())
        self.data_dir = Path(data_dir or pack_data_dir(self.pack_dir))

    def ensure_layout(self, *, seed_from_home: bool = True) -> Path:
        """创建可编辑骨架；bundle 资源仅在缺失时填充；不覆盖现场文件。"""
        self.pack_dir.mkdir(parents=True, exist_ok=True)
        (self.pack_dir / "data").mkdir(parents=True, exist_ok=True)
        (self.pack_dir / "chassis").mkdir(parents=True, exist_ok=True)
        (self.pack_dir / "rtsi").mkdir(parents=True, exist_ok=True)
        if not (self.pack_dir / "pack.json").is_file():
            _write_manifest(self.pack_dir)

        bundle_cfg = _bundle_root() / "config"
        # 示例与 rtsi：不覆盖已有
        for name in ("devices.local.example.yaml", "devices.yaml", "pack.json"):
            src = bundle_cfg / name
            dst = self.pack_dir / name
            if src.is_file() and not dst.is_file():
                _safe_copy(src, dst)
        _copy_tree_fill(bundle_cfg / "rtsi", self.pack_dir / "rtsi", overwrite=False)

        self.data_dir.mkdir(parents=True, exist_ok=True)
        if seed_from_home:
            self._seed_data_from_legacy_home()
        return self.pack_dir

    def _seed_data_from_legacy_home(self) -> None:
        """frozen 首次：若 pack/data 空而 ~/.robot_controller 有数据，迁入。"""
        home = Path.home() / ".robot_controller"
        if not home.is_dir():
            return
        targets = ("missions.db", "arm_poses.json", "arm_sequences.json")
        if any((self.data_dir / n).is_file() for n in targets):
            return
        if not any((home / n).is_file() for n in targets):
            return
        for n in targets:
            _safe_copy(home / n, self.data_dir / n)

    def _ctx(self, client=None, log=None) -> PackContext:
        return PackContext(
            pack_dir=self.pack_dir,
            data_dir=self.data_dir,
            client=client,
            log=log or (lambda _m: None),
        )

    def _modules_for(self, manifest: dict) -> list[tuple[str, PackModule]]:
        out = []
        for mid in manifest.get("modules") or DEFAULT_MODULES:
            mid = str(mid)
            mod = KNOWN_MODULES.get(mid)
            if mod is None:
                continue
            out.append((mid, mod))
        return out

    def export_zip(
        self,
        zip_path: str | Path,
        *,
        client=None,
        log: Optional[Callable[[str], None]] = None,
    ) -> PackResult:
        result = PackResult()
        notes: list[str] = []
        ctx = self._ctx(client, lambda m: notes.append(m))
        self.ensure_layout(seed_from_home=False)
        manifest = _read_manifest(self.pack_dir)
        _write_manifest(self.pack_dir, manifest)

        with tempfile.TemporaryDirectory(prefix="robot_pack_exp_") as tmp:
            dest = Path(tmp) / "pack"
            dest.mkdir(parents=True, exist_ok=True)
            _write_manifest(dest, manifest)
            for mid, mod in self._modules_for(manifest):
                try:
                    mod.collect(ctx, dest)
                    result.modules_done.append(mid)
                except Exception as e:
                    result.modules_skipped.append(mid)
                    result.note(f"{mid}: collect failed: {e}")
            # 保证空 chassis/data 目录进包
            (dest / "data").mkdir(exist_ok=True)
            (dest / "chassis").mkdir(exist_ok=True)
            zip_path = Path(zip_path)
            zip_path.parent.mkdir(parents=True, exist_ok=True)
            if zip_path.exists():
                zip_path.unlink()
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for f in dest.rglob("*"):
                    if f.is_file():
                        zf.write(f, f.relative_to(dest).as_posix())
        for n in notes:
            result.note(n)
        result.note(f"exported to {zip_path}")
        return result

    def import_zip(
        self,
        zip_path: str | Path,
        *,
        client=None,
        log: Optional[Callable[[str], None]] = None,
    ) -> PackResult:
        result = PackResult()
        zip_path = Path(zip_path)
        if not zip_path.is_file():
            result.fail(f"file not found: {zip_path}")
            return result
        with tempfile.TemporaryDirectory(prefix="robot_pack_imp_") as tmp:
            root = Path(tmp)
            with zipfile.ZipFile(zip_path, "r") as zf:
                zf.extractall(root)
            # 支持 zip 根即 pack，或单层子目录
            src = root
            if not (src / "pack.json").is_file():
                subs = [p for p in root.iterdir() if p.is_dir()]
                if len(subs) == 1 and (subs[0] / "pack.json").is_file():
                    src = subs[0]
            return self.load_from_dir(src, client=client, log=log)

    def load_from_dir(
        self,
        src_dir: str | Path,
        *,
        client=None,
        log: Optional[Callable[[str], None]] = None,
    ) -> PackResult:
        result = PackResult()
        notes: list[str] = []
        src = Path(src_dir)
        if not src.is_dir():
            result.fail(f"dir not found: {src}")
            return result
        self.ensure_layout(seed_from_home=False)
        manifest = _read_manifest(src)
        # 合并文件到 pack_dir（整包覆盖模块文件）
        for rel in (
            "pack.json",
            "devices.yaml",
            "devices.local.yaml",
        ):
            if (src / rel).is_file():
                _safe_copy(src / rel, self.pack_dir / rel)
        if (src / "rtsi").is_dir():
            _copy_tree_fill(src / "rtsi", self.pack_dir / "rtsi", overwrite=True)
        if (src / "data").is_dir():
            _copy_tree_fill(src / "data", self.data_dir, overwrite=True)
        if (src / "chassis").is_dir():
            _copy_tree_fill(
                src / "chassis", self.pack_dir / "chassis", overwrite=True
            )
        _write_manifest(self.pack_dir, manifest)

        ctx = self._ctx(client, lambda m: notes.append(m))
        for mid, mod in self._modules_for(manifest):
            try:
                mod.apply(ctx, src)
                result.modules_done.append(mid)
            except Exception as e:
                result.modules_skipped.append(mid)
                result.note(f"{mid}: apply failed: {e}")
        # 未知 module 仅记录
        for mid in manifest.get("modules") or []:
            mid = str(mid)
            if mid not in KNOWN_MODULES:
                result.modules_skipped.append(mid)
                result.note(f"{mid}: unknown module, ignored")
        for n in notes:
            result.note(n)
        result.note("pack loaded into " + str(self.pack_dir))
        return result

    def sync_chassis_from_pack(
        self,
        *,
        client=None,
        log: Optional[Callable[[str], None]] = None,
    ) -> PackResult:
        """仅将包内墙/轨推到已连接底盘（连接后可调用）。"""
        result = PackResult()
        notes: list[str] = []
        if client is None:
            result.fail("no chassis client")
            return result
        ctx = self._ctx(client, lambda m: notes.append(m))
        for mid in ("chassis_walls", "chassis_tracks"):
            mod = KNOWN_MODULES.get(mid)
            if mod is None:
                continue
            try:
                mod.apply(ctx, self.pack_dir)
                result.modules_done.append(mid)
            except Exception as e:
                result.modules_skipped.append(mid)
                result.note(f"{mid}: {e}")
        for n in notes:
            result.note(n)
        return result
