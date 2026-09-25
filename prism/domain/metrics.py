"""v0.3 PR-B 资产度量采集（编辑器内执行；3.7 子集；多签名候选 + tried 证据链）。

只做原始度量：纹理尺寸 / 网格 LOD 与三角数。判名沿用 assets.py 范式：
逐项试候选（unreal 跨 5.0-5.8 漂移），命中记录 `api`，全败记 `tried`。
语义边界：能判类但所有测量候选失效 -> 该资产 error 级 note（不整体崩）；
请求的类全不可测 -> 整体 UE_API_MISMATCH 信封。加载资产进内存是副作用，
批量上限 max_assets（报告层 PR-C 负责节流）。无引擎环境降级 no_engine（结构不变）。
"""
from __future__ import annotations

import os

from .. import envelope
from . import register
from .assets import _asset_data, _class_name, _disk_size, _name_str, _normalize, _registry


def _load_object(unreal, object_path):
    """EditorAssetLibrary(需插件) / load_asset(核心) 两路兜底。"""
    for getter in (
        lambda: unreal.EditorAssetLibrary.load_asset(object_path),
        lambda: unreal.load_asset(object_path),
    ):
        try:
            obj = getter()
        except Exception:
            continue
        if obj is not None:
            return obj
    return None


def _prop(obj, key):
    try:
        return obj.get_editor_property(key)
    except Exception:
        try:
            return getattr(obj, key)
        except Exception:
            return None


def _measure_texture(unreal, obj, tried):
    """返回 {"width","height","memory_bytes"?,"source_disk_bytes"?} 或 None。

    5.4 真机校准（probe_asset_api 实测）：blueprint_get_size_x/y 命中；
    其余为跨版本兜底候选（miss 记入 tried 不崩）。
    """
    m = None
    for name, call in (
        ("blueprint_get_size_x/y", lambda: (obj.blueprint_get_size_x(), obj.blueprint_get_size_y())),
        ("get_surface_sizes", lambda: obj.get_surface_sizes()),
        ("get_surface_size", lambda: obj.get_surface_size(0)),
        ("get_surface_width/height", lambda: (obj.get_surface_width(), obj.get_surface_height())),
        ("get_imported_width/height", lambda: (obj.get_imported_width(), obj.get_imported_height())),
        ("get_array_size", lambda: (obj.get_array_size(), obj.get_array_size())),
    ):
        try:
            out = call()
        except Exception:
            tried.append(name + ":miss")
            continue
        tried.append(name)
        if name == "blueprint_get_size_x/y":
            try:
                w, h = int(out[0]), int(out[1])
            except (TypeError, ValueError, IndexError):
                continue
            if w:
                m = {"width": w, "height": h}
                break
            continue
        s0 = out[0] if isinstance(out, (list, tuple)) and out else out
        try:
            w = getattr(s0, "x", None)
            h = getattr(s0, "y", None)
            if w is None and hasattr(s0, "width"):
                w, h = s0.width, s0.height
            if w:
                m = {"width": int(w), "height": int(h or w)}
                break
        except Exception:
            continue
    if m is None:
        for key in ("source_size", "size_x", "imported_size_x"):
            if _prop(obj, key) is not None:
                tried.append("prop:" + key)
                src = _prop(obj, "source_size")
                sx = src.x if (src is not None and hasattr(src, "x")) else _prop(obj, "size_x")
                sy = src.y if (src is not None and hasattr(src, "y")) else _prop(obj, "size_y")
                if sx:
                    m = {"width": int(sx), "height": int(sy or sx)}
                    break
        else:
            return None
    for name, call, key in (
        ("blueprint_get_memory_size", lambda: int(obj.blueprint_get_memory_size()), "memory_bytes"),
        ("blueprint_get_texture_source_disk_and_memory_size",
         lambda: int(obj.blueprint_get_texture_source_disk_and_memory_size()[0]), "source_disk_bytes"),
        ("prop:max_texture_size", lambda: int(_prop(obj, "max_texture_size")), "max_size"),
    ):
        try:
            v = call()
        except Exception:
            continue
        if v:
            m[key] = v
            tried.append(name)
    # 压缩格式 / VirtualTexture 标记 / CubeArray 层数：跨版本候选，取不到留缺不崩
    cs = _prop(obj, "compression_settings")
    if cs is not None:
        m["compression"] = str(cs)
        tried.append("prop:compression_settings")
    for key in ("virtual_texture_supported", "is_virtual"):
        v = _prop(obj, key)
        if v is not None:
            m["virtual_texture"] = bool(v)
            tried.append("prop:" + key)
            break
    for key in ("array_size", "slices"):
        v = _prop(obj, key)
        if v is not None:
            try:
                v = int(v)
            except (TypeError, ValueError):
                continue
            if v > 1:
                m["array_size"] = v
                tried.append("prop:" + key)
                break
    return m


def _measure_mesh(unreal, obj, tried):
    """返回 {"lods":[{"index","triangles"?,"sections"?}]} 或 None。"""
    nlods = None
    for name in ("get_num_lods",):
        try:
            nlods = int(getattr(obj, name)())
            tried.append(name)
            break
        except Exception:
            continue
    if nlods is None:
        for key in ("lod_info",):
            info = _prop(obj, key)
            if info:
                tried.append("prop:" + key)
                return {"lods": [{"index": i} for i in range(len(info))]}
        return None
    lods = []
    for lod in range(max(0, min(nlods, 8))):
        entry = {"index": lod}
        for name, call in (
            ("get_lod_rendered_triangles", lambda: obj.get_lod_rendered_triangles(lod)),
            ("get_num_triangles", lambda: obj.get_num_triangles(lod)),
        ):
            try:
                entry["triangles"] = int(call())
                tried.append(name)
                break
            except Exception:
                continue
        for name, call in (
            ("get_lod_num_sections", lambda: obj.get_lod_num_sections(lod)),
            ("get_num_sections", lambda: obj.get_num_sections(lod)),
        ):
            try:
                entry["sections"] = int(call())
                tried.append(name + "(lod)")
                break
            except Exception:
                continue
        lods.append(entry)
    return {"lods": lods, "lod_count": nlods}


RUNTIME_HEAVY_MEM_MB = 8.0    # 纹理运行时内存 >= 此值(MB) = 真显存大户
MESH_HEAVY_TRIS = 200000      # 网格 LOD0 三角 >= 此值 = 真吃帧大户（与 rules.mesh_tri warn 同源）
DISK_BLOAT_MIN_DISK_MB = 20.0  # "假大"只对源盘 >= 此值成立
DISK_BLOAT_MAX_RUNTIME_MB = 2.0
DISK_BLOAT_RATIO = 8.0         # 源盘/运行时 >= 此倍数 = 源盘膨胀（MaxSize/压缩已限死运行时）


def _runtime_verdict(disk_bytes, m):
    """纯逻辑：源盘大小 + 度量结果 -> 真大/假大判定（无数据不猜，可离线测）。

    verdict 取值：
    - runtime_heavy   运行时内存/三角超阈值，才是真该优化的大户；
    - disk_only_bloat 源盘大但运行时被限死（典型：225MB 源 PNG，运行时 8KB），
                      对包体/显存影响小，瘦身优先级应降级；
    - normal          运行时可接受；
    - unknown         缺度量数据，不下结论。
    """
    if not isinstance(m, dict):
        return {"verdict": "unknown", "reason": "no_metrics"}
    mem = m.get("memory_bytes")
    try:
        mem = float(mem) if mem is not None else None
    except (TypeError, ValueError):
        mem = None
    if mem is not None:
        mem_mb = mem / 1048576.0
        out = {"runtime_mb": round(mem_mb, 3)}
        if mem_mb >= RUNTIME_HEAVY_MEM_MB:
            out["verdict"] = "runtime_heavy"
            return out
        try:
            disk_bytes = float(disk_bytes) if disk_bytes else None
        except (TypeError, ValueError):
            disk_bytes = None
        if disk_bytes:
            disk_mb = disk_bytes / 1048576.0
            out["disk_mb"] = round(disk_mb, 3)
            if (disk_mb >= DISK_BLOAT_MIN_DISK_MB
                    and mem_mb <= DISK_BLOAT_MAX_RUNTIME_MB
                    and disk_mb / max(mem_mb, 1.0 / 1048576.0) >= DISK_BLOAT_RATIO):
                out["verdict"] = "disk_only_bloat"
                out["capped_by_max_size"] = bool(m.get("max_size"))
                return out
        out["verdict"] = "normal"
        return out
    lods = m.get("lods")
    if lods:
        top = 0
        for l in lods:
            try:
                top = max(top, int(l.get("triangles") or 0))
            except (TypeError, ValueError):
                continue
        if top >= MESH_HEAVY_TRIS:
            return {"verdict": "runtime_heavy", "triangles_top_lod": top}
        if top > 0:
            return {"verdict": "normal", "triangles_top_lod": top}
    return {"verdict": "unknown", "reason": "no_runtime_measure"}


_MEASURERS = {
    "Texture2D": _measure_texture,
    "TextureCube": _measure_texture,
    "VirtualTexture2D": _measure_texture,
    "StaticMesh": _measure_mesh,
}


def _as_path_list(value):
    """Normalize asset_paths into a list.

    Accepts list/tuple as-is, or a string split on comma / newline / semicolon.
    Guards against list(str) exploding a comma-joined string char by char.
    """
    if not value:
        return []
    if isinstance(value, (list, tuple)):
        seq = list(value)
    else:
        import re
        seq = re.split(r"[,\n;]+", str(value))
    out = []
    for x in seq:
        t = str(x).strip()
        if t:
            out.append(t)
    return out


@register
def get_asset_metrics(asset_paths=None, max_assets=20):
    """批量原始度量（纹理 px / 网格 LOD 三角）。items 逐项带 class/metrics/tried/note。"""
    paths = _as_path_list(asset_paths)
    requested = len(paths)
    truncated = requested > int(max_assets)
    paths = paths[:int(max_assets)]
    result = {"items": [], "requested": requested,
              "count": 0, "truncated": truncated, "cap": int(max_assets)}
    try:
        import unreal
    except Exception:
        result["items"] = [{"query": p, "note": "no_engine"} for p in paths]
        result["count"] = len(paths)
        return result
    ar = _registry()
    if ar is None:
        return envelope.make_err(envelope.Code.UE_API_MISMATCH,
                                 "AssetRegistry unavailable: get_asset_registry failed")
    measurable_seen = 0
    measurable_failed = 0
    for p in paths:
        package, object_path, _leaf = _normalize(p)
        item = {"query": package, "package_name": package, "class": None,
                "metrics": None, "tried": [], "note": None,
                "disk_bytes": None, "disk_mb": None,
                "runtime_verdict": "unknown", "is_runtime_heavy": None}
        tried = item["tried"]
        data = _asset_data(ar, unreal, package, object_path)
        if data is None:
            item["note"] = "asset_not_found_in_registry"
            result["items"].append(item)
            continue
        cls = _class_name(unreal, data)
        item["class"] = cls
        try:
            _fp, _sz = _disk_size(unreal, package)
        except Exception:
            _sz = None
        if _sz:
            item["disk_bytes"] = int(_sz)
            item["disk_mb"] = round(int(_sz) / 1048576.0, 3)
        obj = _load_object(unreal, object_path)
        if obj is None:
            item["note"] = "load_failed"
            result["items"].append(item)
            continue
        measurer = _MEASURERS.get(cls)
        if measurer is None:
            for suffix in _MEASURERS:
                if cls and suffix.lower() in cls.lower():
                    measurer = _MEASURERS[suffix]
                    break
        if measurer is None:
            item["note"] = "unmeasurable_class"
        else:
            measurable_seen += 1
            m = measurer(unreal, obj, tried)
            if m is None:
                measurable_failed += 1
                item["note"] = "all_candidates_failed"
            else:
                v = _runtime_verdict(item.get("disk_bytes"), m)
                m["runtime_verdict"] = v
                item["runtime_verdict"] = v["verdict"]
                item["is_runtime_heavy"] = (v["verdict"] == "runtime_heavy")
                item["metrics"] = m
        result["items"].append(item)
    result["count"] = len(result["items"])
    if measurable_seen and measurable_seen == measurable_failed:
        return envelope.make_err(envelope.Code.UE_API_MISMATCH,
                                 "no measurement candidate hit on this engine build; tried=%s" % tried)
    return result



@register
def probe_asset_api(asset_path):
    """校准探针（一次性，PR-B 调试用）：列出对象上含 size/surface/lod/mip 的成员与调用反馈。"""
    try:
        import unreal
    except Exception:
        return {"note": "no_engine"}
    package, object_path, _leaf = _normalize(asset_path)
    obj = _load_object(unreal, object_path)
    if obj is None:
        return {"note": "load_failed", "query": object_path}
    keys = ("size", "surface", "lod", "mip")
    names = [n for n in dir(obj) if any(k in n.lower() for k in keys)]
    feedback = []
    for n in sorted(names)[:40]:
        try:
            attr = getattr(obj, n)
        except Exception as e:
            feedback.append(n + " getattr_err " + str(e)[:80])
            continue
        if callable(attr):
            try:
                r = attr()
                feedback.append(n + "() -> " + str(r)[:160])
            except TypeError as e:
                feedback.append(n + "() TypeError: " + str(e)[:160])
            except Exception as e:
                feedback.append(n + "() Err: " + str(e)[:120])
        else:
            feedback.append(n + " = " + str(attr)[:120])
    cls = None
    try:
        cls = obj.get_class().get_name()
    except Exception:
        pass
    return {"query": object_path, "class": cls, "names": names, "feedback": feedback}

