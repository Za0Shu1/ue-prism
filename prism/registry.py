"""项目发现 registry：bridge 在线时写一条指针，server 零配置时读（DESIGN_v1.0 §3）。

固定、与用户无关的目录（env PRISM_REGISTRY_DIR 覆盖）：
  Windows: %LOCALAPPDATA%/ue-prism/registry ；POSIX: ~/.config/ue-prism/registry
每条指针 json 含 name / slug / project_dir / bus_dir / ue_version / pid / registered_ts。
  · name = 该工程目录下唯一 *.uproject 的主干名（UE/用户都按它认工程）；无/多 .uproject 时回落目录名。
  · 文件名 = <slug>-<hash8(bus_dir)>，hash 由 bus_dir 定；注册时顺带清掉指向同一 bus_dir 的旧名孤儿。
活性不写在这里（会陈旧失真），而是复用 bus_dir 下既有的 heartbeat.json —— server 读时现场算新鲜度。
本模块同时被 bridge（UE 内）import，故严格守 **3.7 子集**、只用标准库。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time

from . import bus

_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")


def registry_dir():
    d = os.environ.get("PRISM_REGISTRY_DIR")
    if d:
        return d
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(base, "ue-prism", "registry")
    cfg = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(cfg, "ue-prism", "registry")


def project_from_bus(bus_dir):
    """从 <proj>/Saved/Prism 反推工程根；不标准路径尽力而为。"""
    try:
        return os.path.dirname(os.path.dirname(os.path.abspath(bus_dir)))
    except Exception:
        return ""


def _norm_dir(p):
    return os.path.abspath(str(p)).replace("\\", "/").lower()


def _project_name(project_dir):
    """目录下唯一 *.uproject 的主干名；0 个或多个 -> None（回落文件夹名）。"""
    try:
        names = [n for n in os.listdir(str(project_dir)) if n.lower().endswith(".uproject")]
    except OSError:
        return None
    if len(names) == 1:
        return os.path.splitext(names[0])[0]
    return None


def _slug(name):
    name = str(name).strip() or "project"
    return _SLUG_RE.sub("_", name)


def _bus_hash(bus_dir_abs):
    return hashlib.sha1(str(bus_dir_abs).encode("utf-8")).hexdigest()[:8]


def _entry_path(slug, bus_dir_abs):
    return os.path.join(registry_dir(), "%s-%s.json" % (slug, _bus_hash(bus_dir_abs)))


def _atomic_write(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, path)


def _purge_other_files_for_bus(bus_dir_abs, keep_path):
    """删除指向同一 bus_dir、但文件名不同的旧指针（改名 / 命名口径切换后的孤儿）。"""
    d = registry_dir()
    keep = os.path.abspath(keep_path)
    target = _norm_dir(bus_dir_abs)
    try:
        names = os.listdir(d)
    except OSError:
        return
    for n in names:
        if not n.endswith(".json") or n.endswith(".tmp"):
            continue
        fp = os.path.join(d, n)
        if os.path.abspath(fp) == keep:
            continue
        try:
            with open(fp, "r", encoding="utf-8") as f:
                e = json.load(f)
        except (OSError, ValueError):
            continue
        if isinstance(e, dict) and _norm_dir(e.get("bus_dir", "")) == target:
            try:
                os.remove(fp)
            except OSError:
                pass


def register(bus_dir, project_dir, ue_version=None):
    """写/刷新一条在线指针（原子）。返回文件路径。失败由调用方兜底，绝不崩编辑器。"""
    os.makedirs(registry_dir(), exist_ok=True)
    bus_abs = os.path.abspath(str(bus_dir))
    proj_abs = os.path.abspath(str(project_dir))
    base = _project_name(proj_abs) or os.path.basename(proj_abs.rstrip("\\/")) or "project"
    entry = {
        "name": base,
        "slug": _slug(base),
        "project_dir": proj_abs.replace("\\", "/"),
        "bus_dir": bus_abs.replace("\\", "/"),
        "ue_version": ue_version,
        "pid": os.getpid(),
        "registered_ts": time.time(),
    }
    path = _entry_path(entry["slug"], bus_abs)
    _atomic_write(path, entry)
    _purge_other_files_for_bus(bus_abs, path)
    return path


def unregister(bus_dir):
    """bridge 主动停止时删除指向该 bus_dir 的指针（不限文件名，幂等）。"""
    target = _norm_dir(bus_dir)
    d = registry_dir()
    try:
        names = os.listdir(d)
    except OSError:
        return
    for n in names:
        if not n.endswith(".json") or n.endswith(".tmp"):
            continue
        fp = os.path.join(d, n)
        try:
            with open(fp, "r", encoding="utf-8") as f:
                e = json.load(f)
        except (OSError, ValueError):
            continue
        if isinstance(e, dict) and _norm_dir(e.get("bus_dir", "")) == target:
            try:
                os.remove(fp)
            except OSError:
                pass


def _load_all():
    d = registry_dir()
    out = []
    try:
        names = sorted(os.listdir(d))
    except OSError:
        return out
    for n in names:
        if not n.endswith(".json") or n.endswith(".tmp"):
            continue
        try:
            with open(os.path.join(d, n), "r", encoding="utf-8") as f:
                e = json.load(f)
            if isinstance(e, dict):
                out.append(e)
        except (OSError, ValueError):
            continue
    return out


def discover(stale_seconds=None):
    """所有指针 + 现场新鲜度。live = bus_dir 心跳年龄 < 阈值（复用 HB_STALE_SECONDS 语义）。"""
    if stale_seconds is None:
        stale_seconds = bus.HB_STALE_SECONDS
    res = []
    for e in _load_all():
        age = bus.heartbeat_age(e.get("bus_dir", ""))
        item = dict(e)
        item["live"] = (age is not None and age <= stale_seconds)
        item["heartbeat_age"] = age
        res.append(item)
    return res


def live_entries(stale_seconds=None):
    return [e for e in discover(stale_seconds) if e["live"]]


def _identities(e):
    return [
        str(e.get("name", "")).lower().replace("\\", "/"),
        str(e.get("slug", "")).lower().replace("\\", "/"),
        os.path.basename(str(e.get("project_dir", ""))).lower(),
        str(e.get("project_dir", "")).lower().replace("\\", "/"),
    ]


def find(project):
    """按 name(.uproject名)/slug/工程目录名/完整路径匹配一条指针；唯一子串命中也算。返回带 live 的 dict 或 None。"""
    if not project:
        return None
    low = str(project).strip().lower().replace("\\", "/")
    all_e = discover()
    for e in all_e:
        if low in _identities(e):
            return e
    matches = [e for e in all_e if any(low in s for s in _identities(e))]
    if len(matches) == 1:
        return matches[0]
    return None


def names(entries=None):
    return sorted(str(e.get("name") or e.get("slug") or "")
                  for e in (entries if entries is not None else discover()))