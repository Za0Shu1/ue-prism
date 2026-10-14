"""v0.3 PR-B 资产度量采集（编辑器内执行；3.7 子集；多签名候选 + tried 证据链）。

只做原始度量：纹理尺寸 / 网格 LOD 与三角数。判名沿用 assets.py 范式：
逐项试候选（unreal 跨 5.0-5.8 漂移），命中记录 `api`，全败记 `tried`。
语义边界：能判类但所有测量候选失效 -> 该资产 error 级 note（不整体崩）；
请求的类全不可测 -> 整体 UE_API_MISMATCH 信封。加载资产进内存是副作用，
批量上限 max_assets（报告层 PR-C 负责节流）。无引擎环境降级 no_engine（结构不变）。
"""
from __future__ import annotations

import os

from .. import envelope, streaming as _st
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


# 5.4 真实工程崩溃复盘：`blueprint_get_texture_source_disk_and_memory_size` 内部走
# FTextureSource::CalcMipSize；贴图若正被编辑器重压缩（改 MaxSize/压缩格式会异步
# BuildTexture），Mip 与 Block.NumMips 会瞬时不一致 -> 引擎 **Assertion failed:
# MipIndex < Block.NumMips (Texture.cpp:2722)** 直接崩掉编辑器，Python 的 try 拦不住断言。
# 源尺寸是导入源数据的属性（改 MaxSize 不动它），所以同一路径只探一次即可；
# 批量写场景另给 probe_source=False 开关，彻底绕开该竞态窗口。
_SOURCE_PROBE_CACHE = {}


def reset_source_probe_cache():
    """清空会话级源尺寸探针缓存（夹具/长跑批测之间隔离状态用）。"""
    _SOURCE_PROBE_CACHE.clear()


def _source_probe(obj, tried, probe_source=True):
    """返回 (disk_bytes, memory_bytes) 或 None；同一路径只调一次引擎探针。"""
    try:
        key = str(obj.get_path_name())
    except Exception:
        key = ""
    if key and key in _SOURCE_PROBE_CACHE:
        cached = _SOURCE_PROBE_CACHE[key]
        tried.append("blueprint_get_texture_source_disk_and_memory_size:cached"
                     if cached else "blueprint_get_texture_source_disk_and_memory_size:failed_cached")
        return cached
    if not probe_source:
        tried.append("source_probe:disabled")
        return None
    try:
        raw = obj.blueprint_get_texture_source_disk_and_memory_size()
        pair = (int(raw[0]), int(raw[1]))
    except Exception:
        if key:
            _SOURCE_PROBE_CACHE[key] = None
        return None
    if key:
        _SOURCE_PROBE_CACHE[key] = pair
    tried.append("blueprint_get_texture_source_disk_and_memory_size")
    return pair


def _measure_texture(unreal, obj, tried, probe_source=True):
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
    if m is None:
        # 5.4 实证：TextureCube 无任何尺寸 API（前面候选全 miss）；不在此早退，
        # 继续采 source 内存/标记，由 _derive_texture_geometry 反推边长。
        m = {}
    for name, call, key in (
        ("blueprint_get_memory_size", lambda: int(obj.blueprint_get_memory_size()), "memory_bytes"),
        ("prop:max_texture_size", lambda: int(_prop(obj, "max_texture_size")), "max_size"),
    ):
        try:
            v = call()
        except Exception:
            continue
        if v:
            m[key] = v
            tried.append(name)
    # 5.4 真机实证：source (disk, memory)=未压缩 RGBA8 总量，不依赖编辑器上传时机；
    # Cube 无尺寸 API，只能由它反推。
    src_pair = _source_probe(obj, tried, probe_source=probe_source)
    if src_pair:
        m["source_disk_bytes"] = src_pair[0]
        m["source_memory_bytes"] = src_pair[1]
    cs = _prop(obj, "compression_settings")
    if cs is not None:
        m["compression"] = str(cs)
        tried.append("prop:compression_settings")
    for key in ("virtual_texture_streaming", "virtual_texture_supported", "is_virtual"):
        v = _prop(obj, key)
        if v is not None:
            m["virtual_texture"] = bool(v)
            tried.append("prop:" + key)
            break
    # 流送池判定要的两项：never_stream（池外常驻）与 lod_group（组级 MaxSize/偏差）
    ns = _prop(obj, "never_stream")
    if ns is not None:
        m["never_stream"] = bool(ns)
        tried.append("prop:never_stream")
    lg = _prop(obj, "lod_group")
    if lg is not None:
        m["lod_group"] = str(lg)
        tried.append("prop:lod_group")
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
    _derive_texture_geometry(obj, m)
    if not m:
        return None
    return m


BC_BYTES_PER_TEXEL = 1.0       # 运行时近似：TC_Default(BC1/BC3) 约 1B/px
MIP_CHAIN_FACTOR = 1.33        # 全 mip 链体积 ~ 1/(1-1/4)


def _faces_of(cls_name, array_size):
    """面数（Cube 6 面 x slice 数）：口径取自 streaming，与池估算同源。"""
    return _st.faces_for(cls_name, array_size)


def _derive_texture_geometry(obj, m):
    """由 source_memory 反推源边长，并按 max_size 折算运行时估算。

    5.4 实证：blueprint_get_size_x/y 在资源上传前返回 32x32 占位，而 Cube 根本没有
    尺寸 API；source_memory_bytes 从载入起就真。估算口径保守（BC 近似），
    仅用于真大/假大判定与 width 缺失时的兜底，标注 size_derived 供消费方辨别。

    源位图不一定是 RGBA8：HDR/RGBA16 是 8B/px、FP32 是 16B/px，Cube 还要摊 6 面。
    一律按 4B/px 反推会把 6x2048^2x8B 的立方图推成 2896 这种假尺寸（5.4 真实工程
    实测），进而编出"钳到 2048 省 96MB"的假收益。故走 streaming.source_side_from_memory
    的多候选 BPP + "2 的幂"校验，并把命中的 BPP 与 exact 标记一起回传，便于核对。
    """
    cls_name = None
    try:
        cls_name = obj.get_class().get_name()
    except Exception:
        pass
    faces = _faces_of(cls_name, m.get("array_size"))
    side = 0
    bpp = 0
    exact = False
    src_mem = m.get("source_memory_bytes")
    if src_mem:
        side, bpp, _faces, exact = _st.source_side_from_memory(src_mem, faces)
    if side:
        m["source_width"] = side
        m["source_bytes_per_texel"] = bpp
        m["source_side_exact"] = bool(exact)
        cur = int(m.get("width") or 0)
        if not cur or cur <= 64:
            # 冷态直读是占位值(<=64)：width 用源反推兜底
            m["width"], m["height"] = side, side
            m["size_derived"] = True
        elif cur != side:
            # 热态直读是 MaxSize/限幅后的有效尺寸（与源尺寸不同）
            m["effective_width"] = cur
    # 运行时估算：有效边长 = min(源边长, max_size 限幅)；全 mip 链、按面数
    est_src = side if side else int(m.get("width") or 0)
    if est_src:
        cap = int(m.get("max_size") or 0)
        if cap > 0:
            est_src = min(est_src, cap)
        m["est_runtime_bytes"] = int(est_src * est_src * faces
                                     * BC_BYTES_PER_TEXEL * MIP_CHAIN_FACTOR)


def _material_slots_from_lods(lods):
    """各 LOD 的 section 数即材质槽数（section 与 material slot 一一对应）；取跨 LOD 最大值。无则 None。"""
    secs = [int(l.get("sections") or 0) for l in (lods or []) if l.get("sections")]
    return max(secs) if secs else None


def _measure_mesh(unreal, obj, tried, probe_source=True):
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
    out = {"lods": lods, "lod_count": nlods}
    for name, call in (
        ("get_lod_used_materials", lambda: len(obj.get_lod_used_materials(0))),
        ("prop:render_materials", lambda: len(_prop(obj, "render_materials"))),
        ("prop:static_materials", lambda: len(_prop(obj, "static_materials"))),
    ):
        try:
            v = call()
        except Exception:
            tried.append(name + ":miss")
            continue
        if v:
            out["material_slots"] = int(v)
            tried.append(name)
            break
    # 5.4 校准：上面候选在本引擎绑定时全 miss，但 get_num_sections 命中 -> 材质槽=最大 section 数
    if "material_slots" not in out:
        ms = _material_slots_from_lods(lods)
        if ms is not None:
            out["material_slots"] = ms
            tried.append("sections->material_slots")
    for name, call in (
        ("get_num_collision_triangles", lambda: obj.get_num_collision_triangles()),
        ("get_collision_number", lambda: obj.get_collision_number()),
    ):
        try:
            v = int(call())
        except Exception:
            tried.append(name + ":miss")
            continue
        if v:
            out["collision_triangles"] = v
            tried.append(name)
            break
    return out


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
    # 5.4 真机校准：常驻内存(blueprint_get_memory_size)在编辑器里可能是未上传/半流送的
    # 占位小值。运行时规模走 streaming.runtime_ledger（实测优先，实测没上传才按格式倍率
    # 折算估算），与池估算同源：同一张 TC_HDR 立方图不能在 metrics 里 192MB、
    # 在 scan_texture_streaming 里 31.9MB（实测 8B/px vs BC 近似 1B/px 差 6 倍）。
    ledger = _st.runtime_ledger(m)
    runtime = ledger["runtime_bytes"]
    try:
        mem = float(m.get("memory_bytes") or 0)
    except (TypeError, ValueError):
        mem = 0.0
    if runtime > 0:
        mem_mb = runtime / 1048576.0
        out = {"runtime_mb": round(mem_mb, 3),
               "runtime_basis": ledger["runtime_basis"],
               "bytes_per_texel": ledger["runtime_bytes_per_texel"]}
        if mem and runtime != mem:
            out["resident_mb"] = round(mem / 1048576.0, 3)
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


def _u_bool(value, default=True):
    """总线/MCP 可能把 bool 送成字符串（"false"/"0"），显式归一，未知值走 default。"""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "y", "on"):
        return True
    if text in ("0", "false", "no", "n", "off", ""):
        return False
    return default


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
def get_asset_metrics(asset_paths=None, max_assets=20, probe_source=True):
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
            m = measurer(unreal, obj, tried, probe_source=_u_bool(probe_source))
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
    keys = ("size", "surface", "lod", "mip", "collision", "material",
            "stream", "format", "memory", "source",
            "section", "tri", "num", "vertex", "body", "setup", "simple", "agggeom")
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

