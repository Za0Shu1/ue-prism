"""LOD 阶梯只读诊断（复盘 #13 立项；编辑器内执行）。

探针已把可达面钉死（docs/TESTING.md「LOD 族立项前取证」，5.3.2/5.4.4 逐条一致）：
- 每级 `ScreenSize` 在 UE Python 上**读不到也写不到**（六候选属性面全 miss，数据在
  `FStaticMeshRenderData`，非 UPROPERTY）——所以本族不改阈值，改阈值的产品承诺一律不给。
- `is_lod_screen_size_auto_computed()` 可读，是诚实判据的根：阈值由引擎自动算时，
  「美术阈值设得不合理」这类结论不成立，只能回传标志而不是猜。
- 每平台 Min LOD 可读写（`get/set_minimum_lod_for_platform`，实测写后回读相符）——
  这才是能做成的动作面，交给 `opt_mesh_set_min_lod`（写·双钥）。

本模块只做只读阶梯诊断：LOD 数、每级三角数、降比、Nanite 状态、平台 Min LOD 覆盖缺口，
并给**三角数削减潜力**（帧时间口径）。这条账不是 MB，不能与纹理池那本账相加；实例数与
可见性不在本模块口径内（关卡实例面走 `analyze_instancing` / `list_level_actors(compose)`）。
"""
from __future__ import annotations

from .. import envelope
from . import metrics as _m
from . import opt_nanite as _n
from . import optutil as _u
from . import register

DEFAULT_MIN_TRIANGLES = 200000    # 与 rules.PROFILES.mesh_tri / metrics.MESH_HEAVY_TRIS 同源
WEAK_RATIO = 0.6                  # LOD1/LOD0 高于此值 = 阶梯没降下来
PLATFORM_KEYS = ("ALL", "Windows", "WindowsNoEditor", "Android", "IOS")
CATEGORIES = ("nanite_managed", "no_lod_highpoly", "weak_lod_chain",
              "min_lod_actionable", "ok", "unreadable")


def _int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _try_call(obj, name, arg_shapes):
    """同名方法按多种参数形态试（探针实证：UE 绑定形态随重载/默认值漂移）。

    返回 (value, via, tried)；`via` 原样回传，跨版本兼容矩阵就看哪一形态命中。
    """
    tried = []
    fn = getattr(obj, name, None)
    if not callable(fn):
        return None, None, [{"sig": name, "err": "absent"}]
    for args, label in arg_shapes:
        try:
            return fn(*args), name + ":" + label, tried
        except Exception as exc:
            tried.append({"sig": "%s(%s)" % (name, label), "err": type(exc).__name__})
    return None, None, tried


def _map_to_plain(mp):
    """UE TMap 的 python 面（探针实测回传 Map 对象）多路转成 {str: int}；全失败返回 None。"""
    if mp is None:
        return None
    if isinstance(mp, dict):
        cand = mp
    else:
        cand = None
        for conv in (lambda: dict(mp),
                     lambda: {k: mp[k] for k in mp.keys()},
                     lambda: getattr(mp, "to_dict", lambda: None)()):
            try:
                got = conv()
            except Exception:
                continue
            if isinstance(got, dict):
                cand = got
                break
        if cand is None:
            return None
    out = {}
    for k, v in cand.items():
        key = str(k)
        if not key:
            continue
        out[key] = _int(v, -1)
    return out


def read_auto_compute(obj):
    """(bool|None, via, tried)：阈值是否由引擎自动算。读不到返回 None（不猜）。"""
    v, via, tried = _try_call(obj, "is_lod_screen_size_auto_computed",
                              [((), "noarg"), ((0,), "lod0")])
    if v is None:
        return None, None, tried
    return bool(v), via, tried


def read_min_lod(obj):
    """每平台强制起始 LOD：{平台: 级号}，-1/缺失 = 没覆盖。

    两路：整张 map（`get_minimum_lod_for_platforms`，探针实证是 TMap）与逐平台
    （`get_minimum_lod_for_platform(key)`，探针实证读写都通）。map 转不出字典时退逐平台。
    """
    tried = []
    out = {}
    v, map_via, mt = _try_call(obj, "get_minimum_lod_for_platforms", [((), "noarg")])
    tried.extend(mt)
    plain = _map_to_plain(v)
    if plain:
        out.update(plain)
    per_via = None
    for key in PLATFORM_KEYS:
        v2, via2, t2 = _try_call(obj, "get_minimum_lod_for_platform", [((key,), key)])
        tried.extend(t2)
        if v2 is None:
            continue
        if per_via is None:
            per_via = via2
        if key not in out:
            out[key] = _int(v2, -1)
    hit = map_via if plain else per_via
    return out, hit, tried


def judge(num_lods, lods, nanite, auto_computed, min_lod, min_tri, weak_ratio=WEAK_RATIO):
    """纯逻辑分类（不碰 unreal，L0 直接钉判据）。

    返回 (category, flags, ratio, target_lod, potential_triangles)。
      nanite_managed    Nanite 已开：细节由 Nanite 自己管，Min LOD 强制降级不划算，不报候选。
      no_lod_highpoly   只有 1 级且面数超阈值：最危险，但引擎侧生成 LOD 不在本族承诺内，
                        只能给「需美术补 LOD」的动作，不给收益数字（给了就是编）。
      weak_lod_chain    有阶梯但 LOD1/LOD0 > weak_ratio：第一刀没降下来。
      min_lod_actionable 阶梯可用、面数超阈值、且平台没有 Min LOD 覆盖 -> 可强制降级的靶。
      ok                面数低于阈值或已覆盖。
      unreadable        LOD 面读不到（载入失败、API 漂移，或级数可读却全测不出三角数）。
    `potential_triangles` 只在可强制降级时给：LOD0 - LOD(target_lod) 的三角数差，
    是「单实例单帧」口径，不含实例数/可见性——别当 MB 记账，也别与池那本账相加。
    """
    flags = []
    tris = []
    for row in (lods or []):
        t = row.get("triangles") if isinstance(row, dict) else None
        tris.append(_int(t, 0) if t is not None else None)
    known = [t for t in tris if t is not None]
    tri0 = known[0] if known else 0
    if num_lods is None:
        num_lods = len(tris) if tris else None

    if auto_computed is True:
        flags.append("screen_size_auto_computed")
    elif auto_computed is None:
        flags.append("auto_compute_unknown")
    if nanite is None:
        flags.append("nanite_unknown")

    covered = False
    for k, v in (min_lod or {}).items():
        if _int(v, -1) >= 0:
            covered = True
    if not covered:
        flags.append("min_lod_unset")

    ratio = None
    if len(known) >= 2 and known[0]:
        ratio = round(float(known[1]) / float(known[0]), 4)

    target_lod = None
    potential = 0
    if num_lods and num_lods >= 2:
        target_lod = num_lods - 1
        # 只认「建议的那一级」自己的实测三角数：metrics 的窗口最深到 LOD7，
        # 拿"最深已测级"兜底会给出与建议级不符的数字——宁缺勿假。
        low = tris[target_lod] if target_lod < len(tris) and tris[target_lod] is not None else None
        if tri0 and low is not None:
            potential = max(0, tri0 - int(low))

    if nanite is True:
        category = "nanite_managed"
        potential = 0
        target_lod = None
    elif num_lods is None or (num_lods <= 1 and not tri0):
        category = "unreadable"
        potential = 0
    elif num_lods <= 1:
        category = "no_lod_highpoly" if tri0 >= min_tri else "ok"
        potential = 0
    elif not known:
        # 级数读得到、每级三角数全读不到：高面数与否无从判断，不拿 ok 装干净
        category = "unreadable"
        potential = 0
        target_lod = None
    elif tri0 < min_tri:
        category = "ok"
        potential = 0
    elif ratio is not None and ratio > weak_ratio:
        category = "weak_lod_chain"
    elif covered:
        category = "ok"
        potential = 0
    else:
        category = "min_lod_actionable"
    if category in ("weak_lod_chain", "min_lod_actionable") and not potential:
        flags.append("reduction_unmeasured")   # 报了候选但报不出削减量：宁缺勿假
    return category, flags, ratio, target_lod, potential


@register
def scan_mesh_lod(folder="/Game", map=None, paths=None,
                  min_triangles=DEFAULT_MIN_TRIANGLES, max_assets=200,
                  top=40, offset=0):
    """LOD 阶梯只读诊断：谁没阶梯、谁阶梯降不下来、谁能用『每平台 Min LOD』直接省三角。

    按 folder / 关卡闭包 / 显式 paths 枚举 StaticMesh，逐个给 LOD 数、每级三角数、
    LOD1/LOD0 降比、Nanite 状态、`is_lod_screen_size_auto_computed`（阈值是不是引擎自动算的）、
    每平台 Min LOD 覆盖，并把可强制降级的网格汇成 `candidates.force_min_lod`
    （`opt_mesh_set_min_lod` 的输入）。

    口径与边界（重要）：
    - 收益是**三角数**（单实例单帧），不是 MB，不能与纹理池那本账相加；实例数/可见性不在这里。
    - 每级 `ScreenSize` 在 UE Python 上读不到也写不到（5.3/5.4 探针实证），本工具不改阈值，
      也不给「阈值不合理」这类结论；`screen_size_auto_computed` 只陈述阈值由引擎算。
    - Nanite 已开的网格不参与降级候选（细节由 Nanite 管，强制 Min LOD 反损画质）。
    - 只有 1 级 LOD 的高面数网格最危险，但补 LOD 不在本族承诺内 -> 只给动作不给收益数字。
    无引擎时结构键常驻、note=no_engine。
    """
    top = max(1, _int(top, 40))
    min_tri = _int(min_triangles, DEFAULT_MIN_TRIANGLES)
    resolved = _u.resolve_targets(paths=paths, folder=folder, map_path=map,
                                  terms=_u.MESH_TERMS, limit=max_assets, offset=offset)
    summary = {"measured": 0, "unmeasured": 0, "tri_total": 0, "potential_triangles": 0}
    for cat in CATEGORIES:
        summary[cat] = 0
    summary["auto_compute_true"] = 0
    summary["min_lod_covered"] = 0
    result = {"scope": {"folder": folder, "map": map, "source": resolved["source"]},
              "thresholds": {"min_triangles": min_tri, "weak_ratio": WEAK_RATIO},
              "summary": summary, "items": [],
              "candidates": {"no_lod_highpoly": [], "weak_lod_chain": [], "force_min_lod": []},
              "total": 0, "truncated": False, "cap": top,
              "api": {"hits": {}, "tried": []},
              "note": resolved["note"], "rejected": resolved["rejected"]}
    if not resolved["found_registry"]:
        return result

    import unreal
    tried = []
    hits = {}
    rows = []
    for target in resolved["targets"]:
        pkg = target["package"]
        cls = target["class"]
        obj = _u.load_object(unreal, target["object_path"])
        if obj is None:
            rows.append({"package": pkg, "class": cls, "category": "unreadable",
                         "flags": ["load_failed"], "num_lods": None, "lods": [],
                         "triangles": None})
            summary["unmeasured"] += 1
            summary["unreadable"] += 1
            continue
        mtried = []
        try:
            metrics = _m._measure_mesh(unreal, obj, mtried)
        except Exception:
            metrics = None
        lods = (metrics or {}).get("lods") or []
        nv, num_via, ntried = _try_call(obj, "get_num_lods", [((), "noarg")])
        num_lods = _int(nv, 0) if nv is not None else (len(lods) if lods else None)
        auto, auto_via, at = read_auto_compute(obj)
        min_lod, min_via, mt = read_min_lod(obj)
        nanite, nanite_api, nt = _n._read_nanite(obj)
        for label in (num_via, auto_via, min_via, nanite_api):
            if label:
                hits[label] = hits.get(label, 0) + 1
        tried.extend(mtried)
        tried.extend(ntried + at + mt + nt)
        category, flags, ratio, target_lod, potential = judge(
            num_lods, lods, nanite, auto, min_lod, min_tri)
        tri0 = None
        for row in lods:
            if isinstance(row, dict) and row.get("triangles") is not None:
                tri0 = _int(row.get("triangles"), 0)
                break
        rows.append({"package": pkg, "class": cls, "category": category, "flags": flags,
                     "num_lods": num_lods, "lods": lods[:8],
                     "triangles": tri0, "lod1_ratio": ratio,
                     "nanite_enabled": nanite, "auto_computed": auto,
                     "min_lod_by_platform": min_lod,
                     "suggested_min_lod": target_lod,
                     "potential_triangles": potential,
                     "disk_mb": round(target["disk_bytes"] / 1048576.0, 3)
                                if target.get("disk_bytes") else None})
        summary["measured"] += 1
        summary[category] = summary.get(category, 0) + 1
        if tri0:
            summary["tri_total"] += tri0
        if potential:
            summary["potential_triangles"] += int(potential)
        if auto is True:
            summary["auto_compute_true"] += 1
        if any(_int(v, -1) >= 0 for _k, v in (min_lod or {}).items()):
            summary["min_lod_covered"] += 1

    unread = [r for r in rows if r.get("category") == "unreadable"]
    if unread and len(unread) == len(rows):
        return envelope.make_err(envelope.Code.UE_API_MISMATCH,
                                 "LOD 面全数读不到（get_num_lods / is_lod_screen_size_auto_computed "
                                 "/ minimum_lod_for_platform 均不命中）——5.0-5.8 漂移？")
    summary["unmeasured"] = len(unread)

    rows.sort(key=lambda r: (-int(r.get("potential_triangles") or 0),
                             -int(r.get("triangles") or 0), str(r.get("package"))))
    result["total"] = len(rows)
    result["truncated"] = len(rows) > top
    result["items"] = rows[:top]
    result["api"] = {"hits": hits, "tried": tried[-40:]}

    def _pack(cats, extra_fields=()):
        out = []
        for r in rows:
            if r.get("category") not in cats:
                continue
            entry = {"package": r["package"], "num_lods": r.get("num_lods"),
                     "triangles": r.get("triangles")}
            for f in extra_fields:
                entry[f] = r.get(f)
            out.append(entry)
        return out[:40]

    result["candidates"] = {
        "no_lod_highpoly": _pack(("no_lod_highpoly",)),
        "weak_lod_chain": _pack(("weak_lod_chain",), ("lod1_ratio", "potential_triangles")),
        "force_min_lod": _pack(("weak_lod_chain", "min_lod_actionable"),
                               ("suggested_min_lod", "potential_triangles")),
    }
    if summary["min_lod_actionable"] or summary["weak_lod_chain"]:
        result["note"] = ("%d 个网格可用『每平台 Min LOD』强制降级（合计潜在削减 %d 三角，"
                          "单实例单帧口径）；用 opt_mesh_set_min_lod 先出 dry-run 计划。"
                          % (summary["min_lod_actionable"] + summary["weak_lod_chain"],
                             summary["potential_triangles"]))
    elif summary["no_lod_highpoly"]:
        result["note"] = ("高面数无阶梯网格 %d 个：补 LOD 不在本工具承诺内，需美术重导或"
                          "在建模端减面；本工具不给其收益数字。" % summary["no_lod_highpoly"])
    else:
        result["note"] = "无可强制降级的网格（阈值 min_triangles=%d 可调）" % min_tri
    if result["truncated"]:
        result["scan_note"] = ("本窗仅度量 %d 个网格（候选与两本账为该窗口内值），"
                               "翻页 offset=%d 可继续。" % (len(rows), offset + max_assets))
    return result
