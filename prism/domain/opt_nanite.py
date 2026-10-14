"""Nanite：只读候选/冲突分析 + 批量开关（v1.5 PR-1/PR-3；编辑器内执行）。

对应 TODO Feature 2。实测痛点：半透明材质挂 Nanite 网格 -> UE 静默降级 + 刷日志
（某 5.3 真实工程 ~14,846 条 Warning 几乎全是这一条）。所以两件事分开做：
- scan_nanite_candidates：列"该开的"（高面数/未开/材质不半透明）与"已开却半透明"的冲突项；
- opt_mesh_set_nanite：批量开/关（写·双钥），命中半透明材质默认 skipped 并附 blend mode；
- fix_nanite_translucent：把"已开 Nanite 且挂半透明材质"的网格关掉，消警告回正常渲染。

跨版本属性漂移（5.0/5.1 bSupportsNanite -> 5.3 NaniteSettings.enabled -> 5.4 再变）
一律多候选 + 回读校验；写不进就 failed + tried，绝不假成功。
材质需 bUsedWithNanite 才真正走 Nanite：本模块只探测并汇报需改的材质，
不擅自动材质（那是另一类写操作，留待后续 PR）。
"""
from __future__ import annotations

from .. import envelope
from . import metrics as _m
from . import optutil as _u
from . import register

NANITE_FLAT_PROPS = ("nanite_enabled", "supports_nanite")
NANITE_STRUCT = "nanite_settings"
NANITE_STRUCT_MEMBERS = ("enabled", "is_enabled")
DEFAULT_MIN_TRIANGLES = 200000   # 与 rules.PROFILES.mesh_tri warn 同源（PC）
NANITE_USAGE_PROPS = ("used_with_nanite", "b_used_with_nanite")


def _int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _read_nanite(obj):
    """(bool|None, 命中签名, tried)。平坦属性优先，退化到 5.3 的 nanite_settings.enabled。"""
    value, hit, tried = _u.get_prop_any(obj, NANITE_FLAT_PROPS)
    if value is not None:
        return bool(value), hit, tried
    ns = _u.prop(obj, NANITE_STRUCT)
    if ns is not None:
        for key in NANITE_STRUCT_MEMBERS:
            member = _u.prop(ns, key)
            if member is not None:
                return bool(member), "%s.%s" % (NANITE_STRUCT, key), tried
        tried.append({"prop": NANITE_STRUCT, "err": "no_enabled_member"})
    return None, None, tried


def _write_nanite(unreal, obj, want):
    """写 Nanite 开关（多签名 + 回读校验）。返回 (ok, api, tried)。"""
    want = bool(want)
    ok, hit, tried = _u.set_prop_any(obj, NANITE_FLAT_PROPS, want)
    if ok:
        return True, hit, tried
    ns = _u.prop(obj, NANITE_STRUCT)
    if ns is None:
        return False, None, tried
    for key in NANITE_STRUCT_MEMBERS:
        try:
            ns.set_editor_property(key, want)
        except Exception as exc:
            tried.append({"sig": "%s.set_editor_property(%s)" % (NANITE_STRUCT, key),
                          "err": type(exc).__name__})
            continue
        ok2, hit2, tried2 = _u.set_prop_any(obj, (NANITE_STRUCT,), ns)
        if not ok2:
            tried.extend(tried2)
            continue
        check, _h, _t = _read_nanite(obj)
        if check is want:
            return True, "%s.%s + %s" % (NANITE_STRUCT, key, hit2), tried + tried2
        tried.append({"sig": "verify:%s.%s" % (NANITE_STRUCT, key),
                      "err": "value_not_applied", "got": str(check)})
    return False, None, tried


def _triangles(unreal, obj, tried):
    metrics = None
    try:
        metrics = _m._measure_mesh(unreal, obj, tried)
    except Exception:
        metrics = None
    if not metrics:
        return None, None
    lods = metrics.get("lods") or []
    top = 0
    for lod in lods:
        try:
            top = max(top, int(lod.get("triangles") or 0))
        except (TypeError, ValueError):
            continue
    return (top if top else None), metrics.get("material_slots")


def _material_view(unreal, obj):
    """网格材质清单 + 半透明判定 + 需补 used_with_nanite 的计数（全 best-effort）。

    材质槽取不到、或 blend_mode 读不到/认不出，一律 blend_unreadable=True：
    上层（opt_mesh_set_nanite / fix_nanite_translucent）按"未知"保守处理，不开也不关。

    5.4 实证：used_with_nanite 挂在 UMaterial 上，static_materials 取回的却是
    MaterialInterface，在接口上直读恒 miss（真机表现=整份清单一律 null）。
    读不到必须先 base_material() 解包基类材质再试一次，仍读不到才降级成 null。
    """
    mats, api, _sub, unreadable = _u.mesh_materials(unreal, obj)
    rows = []
    translucent = False
    need_usage = 0
    for mat in mats:
        mode, trans, _hit, readable = _u.blend_mode_of(unreal, mat)
        if not readable:
            unreadable = True
        if trans:
            translucent = True
        package = None
        try:
            package = str(mat.get_path_name()).split(".")[0]
        except Exception:
            package = None
        used, usage_api, _ut = _u.get_prop_any(mat, NANITE_USAGE_PROPS)
        if used is None:
            base, base_api, _btried = _u.base_material(unreal, mat)
            if base is not None and base is not mat:
                used, prop_hit, _ut = _u.get_prop_any(base, NANITE_USAGE_PROPS)
                if prop_hit:
                    usage_api = "%s>%s" % (base_api, prop_hit)
        used = bool(used) if used is not None else None
        if used is not True:
            need_usage += 1
        rows.append({"material": package, "blend_mode": mode, "translucent": trans,
                     "used_with_nanite": used, "usage_api": usage_api})
    return {"api": api, "materials": rows, "translucent": translucent,
            "blend_unreadable": unreadable, "materials_need_used_with_nanite": need_usage}


@register
def scan_nanite_candidates(folder="/Game", map=None, paths=None,
                           min_triangles=DEFAULT_MIN_TRIANGLES, max_assets=200,
                           top=40, offset=0):
    """谁该开 Nanite、谁开了反而出问题（只读）。

    返回 items（按三角数降序，cap=top）+ summary（分类计数）+ candidates
    （enable / conflicts / blocked_by_translucent / materials_need_used_with_nanite）
    + total/truncated/cap + tried。分类：
      already_ok / enable_candidate / conflict_translucent / blocked_translucent /
      low_complexity / unknown_api
    无引擎时结构键常驻、note=no_engine。
    """
    top = max(1, _int(top, 40))
    min_tri = _int(min_triangles, DEFAULT_MIN_TRIANGLES)
    resolved = _u.resolve_targets(paths=paths, folder=folder, map_path=map,
                                  terms=_u.MESH_TERMS, limit=max_assets, offset=offset)
    result = {"scope": {"folder": folder, "map": map, "source": resolved["source"]},
              "items": [], "summary": {"measured": 0, "unmeasured": 0, "tri_total": 0,
                                       "already_ok": 0, "enable_candidate": 0,
                                       "conflict_translucent": 0, "blocked_translucent": 0,
                                       "low_complexity": 0, "unknown_api": 0,
                                       "materials_need_used_with_nanite": 0},
              "candidates": {"enable": [], "conflicts": [], "blocked_by_translucent": [],
                             "materials_need_used_with_nanite": []},
              "thresholds": {"min_triangles": min_tri},
              "total": 0, "truncated": False, "cap": top,
              "note": resolved["note"], "tried": [], "rejected": resolved["rejected"]}
    if not resolved["found_registry"]:
        return result

    import unreal
    tried = []
    rows = []
    for target in resolved["targets"]:
        obj = _u.load_object(unreal, target["object_path"])
        if obj is None:
            rows.append({"package": target["package"], "class": target["class"],
                         "category": "unknown_api", "note": "load_failed"})
            continue
        tris, slots = _triangles(unreal, obj, tried)
        nanite, api, sub = _read_nanite(obj)
        view = _material_view(unreal, obj)
        if nanite is None:
            category = "unknown_api"
        elif nanite and view["translucent"]:
            category = "conflict_translucent"
        elif nanite:
            category = "already_ok"
        elif view["translucent"]:
            category = "blocked_translucent"
        elif tris and tris >= min_tri:
            category = "enable_candidate"
        elif tris is None:
            category = "unknown_api"
        else:
            category = "low_complexity"
        modes = sorted(set(r["blend_mode"] for r in view["materials"] if r.get("blend_mode")))
        rows.append({"package": target["package"], "class": target["class"],
                     "triangles": tris, "material_slots": slots, "nanite_enabled": nanite,
                     "nanite_prop": api, "category": category,
                     "translucent": view["translucent"], "blend_modes": modes,
                     "blend_unreadable": view["blend_unreadable"],
                     "materials": view["materials"][:8],
                     "materials_need_used_with_nanite": view["materials_need_used_with_nanite"],
                     "disk_mb": round(target["disk_bytes"] / 1048576.0, 3) if target.get("disk_bytes") else None})

    summary = result["summary"]
    for row in rows:
        cat = row.get("category")
        if cat in summary:
            summary[cat] += 1
        if row.get("triangles"):
            summary["tri_total"] += int(row["triangles"])
        if row.get("nanite_enabled") is not None:
            summary["measured"] += 1
        else:
            summary["unmeasured"] += 1
        summary["materials_need_used_with_nanite"] += int(row.get("materials_need_used_with_nanite") or 0)

    rows.sort(key=lambda r: (-(r.get("triangles") or 0), str(r.get("package"))))
    result["total"] = len(rows)
    result["truncated"] = len(rows) > top
    result["items"] = rows[:top]
    result["tried"] = tried[-40:]

    def _pack(categories):
        out = []
        for r in rows:
            if r.get("category") not in categories:
                continue
            entry = {"package": r["package"], "triangles": r.get("triangles"),
                     "material_slots": r.get("material_slots")}
            if "conflict_translucent" in categories:
                entry["blend_modes"] = r.get("blend_modes")
                entry["advice"] = "关 Nanite（fix_nanite_translucent）或改材质混合模式"
            elif "blocked_translucent" in categories:
                entry["blend_modes"] = r.get("blend_modes")
                entry["advice"] = "半透明材质开 Nanite 会静默降级，不建议开"
            else:
                entry["nanite_enabled"] = r.get("nanite_enabled")
                if r.get("materials_need_used_with_nanite"):
                    entry["materials_need_used_with_nanite"] = r["materials_need_used_with_nanite"]
            out.append(entry)
        return out[:40]

    result["candidates"] = {
        "enable": _pack(("enable_candidate",)),
        "conflicts": _pack(("conflict_translucent",)),
        "blocked_by_translucent": _pack(("blocked_translucent",)),
        "materials_need_used_with_nanite": [
            {"package": r["package"], "count": r["materials_need_used_with_nanite"]}
            for r in rows if r.get("materials_need_used_with_nanite")][:40],
    }
    if summary["conflict_translucent"]:
        result["note"] = ("%d 个网格已开 Nanite 却挂半透明材质：UE 会静默降级并刷 Warning。"
                          "用 fix_nanite_translucent 先出 dry-run 计划。"
                          % summary["conflict_translucent"])
    elif not summary["enable_candidate"]:
        result["note"] = "无高面数未开 Nanite 的候选（阈值 min_triangles=%d 可调）" % min_tri
    return result


@register
def opt_mesh_set_nanite(paths=None, folder="/Game", map=None, enabled=True,
                        skip_translucent=True, limit=200, offset=0,
                        dry_run=True, confirm=False):
    """批量开/关 Nanite（写·双钥）。半透明材质默认拦截（skip_translucent）。

    收益是 drawcall/帧时间与日志噪音，不是磁盘/显存 MB，故 savings_mb 恒为 0，
    判断依据看行内 triangles / blend_modes / materials_need_used_with_nanite。
    """
    want = _u.to_bool(enabled, True)
    block = _u.to_bool(skip_translucent, True)

    def inspect(unreal, obj, target, tried):
        nanite, api, sub = _read_nanite(obj)
        if nanite is None:
            return {"kind": "unknown", "note": "读写不到 Nanite 开关（5.0-5.8 签名漂移？）",
                    "tried": sub[-6:], "before": None, "after": want}
        if nanite is want:
            return {"kind": "no_op", "before": nanite, "after": nanite,
                    "reason": "已是目标状态(%s)" % api}
        tris, slots = _triangles(unreal, obj, tried)
        view = _material_view(unreal, obj)
        modes = sorted(set(r["blend_mode"] for r in view["materials"] if r.get("blend_mode")))
        detail = {"triangles": tris, "material_slots": slots, "blend_modes": modes,
                  "materials_need_used_with_nanite": view["materials_need_used_with_nanite"],
                  "nanite_prop": api}
        if want and view["translucent"] and block:
            return dict(detail, kind="skip", before=nanite, after=nanite,
                        reason="半透明材质(%s)开 Nanite 会静默降级+刷警告；"
                               "确要强行开带 skip_translucent=False" % ",".join(modes))
        if want and view["blend_unreadable"] and not view["translucent"]:
            return dict(detail, kind="skip", before=nanite, after=nanite,
                        reason="blend_mode 读不到，保守不动（跨版本属性漂移）；"
                               "确要开带 skip_translucent=False")
        return dict(detail, kind="change", before=nanite, after=want,
                    savings_mb=0.0, triangles=tris, materials=view["materials"][:8])

    def write(unreal, obj, decision, target):
        return _write_nanite(unreal, obj, want)

    return _u.drive("opt_mesh_set_nanite", _u.MESH_TERMS, paths, folder, map, limit, offset,
                    dry_run, confirm, inspect, write,
                    extra={"enabled": want, "skip_translucent": block})


@register
def fix_nanite_translucent(paths=None, folder="/Game", map=None, limit=200, offset=0,
                           dry_run=True, confirm=False):
    """把"已开 Nanite 却挂半透明材质"的网格关掉（写·双钥；TODO Feature 2b）。

    只关这一类冲突，不碰正常 Nanite 网格；材质读不到时跳过（不误关）。
    关闭是降级而非损坏：网格仍按原 LOD 正常渲染，警告随之消失。
    """
    def inspect(unreal, obj, target, tried):
        nanite, api, sub = _read_nanite(obj)
        if nanite is None:
            return {"kind": "unknown", "note": "读写不到 Nanite 开关",
                    "tried": sub[-6:], "before": None, "after": False}
        if not nanite:
            return {"kind": "no_op", "before": False, "after": False,
                    "reason": "未开 Nanite，无冲突"}
        tris, slots = _triangles(unreal, obj, tried)
        view = _material_view(unreal, obj)
        modes = sorted(set(r["blend_mode"] for r in view["materials"] if r.get("blend_mode")))
        detail = {"triangles": tris, "material_slots": slots, "blend_modes": modes,
                  "nanite_prop": api}
        if not view["translucent"]:
            if view["blend_unreadable"]:
                return dict(detail, kind="skip", before=True, after=True,
                            reason="blend_mode 读不到，保守不动")
            return dict(detail, kind="skip", before=True, after=True,
                        reason="材质不半透明，保留 Nanite")
        return dict(detail, kind="change", before=True, after=False, savings_mb=0.0,
                    triangles=tris, materials=view["materials"][:8],
                    reason="半透明材质(%s)与 Nanite 冲突 -> 关闭" % ",".join(modes))

    def write(unreal, obj, decision, target):
        return _write_nanite(unreal, obj, False)

    out = _u.drive("fix_nanite_translucent", _u.MESH_TERMS, paths, folder, map, limit, offset,
                   dry_run, confirm, inspect, write,
                   extra={"target_conflict": "nanite + translucent"})
    if isinstance(out, dict) and out.get("ok"):
        res = out["result"]
        if res["dry_run"] and not res["planned"]:
            res["note"] = "未发现 Nanite+半透明冲突（或该窗口内没有）。无写操作计划。"
    return out
