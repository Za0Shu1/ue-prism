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

import io
import json
import os
import time

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


# ---------------------------------------------------------------------------
# 1b：每平台 Min LOD 强制降级（写·双钥）。阈值面探针实证改不了，写面只走这一条正门。
# ---------------------------------------------------------------------------

OP = "opt_mesh_set_min_lod"
MANIFEST_VERSION = 1


def _to_int(value, default=None):
    """归一整数：None/空串/非法 -> default（区分"没给"与"给了 0"，这里 0 是合法值）。"""
    if value is None:
        return default
    if isinstance(value, bool):
        return default
    text = str(value).strip()
    if not text:
        return default
    try:
        return int(float(text))
    except (TypeError, ValueError):
        return default


def _write_json_atomic(path, payload):
    """半截清单等于没有回滚：同 bus 口径的 .tmp + os.replace。"""
    folder = os.path.dirname(str(path))
    if folder and not os.path.isdir(folder):
        os.makedirs(folder)
    tmp = str(path) + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=1)
    os.replace(tmp, str(path))
    return str(path)


def _read_manifest(path):
    try:
        with io.open(str(path), "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except Exception:
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("entries"), list):
        return None
    return payload


def _manifest_path(unreal, given, op):
    if given:
        return str(given)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    root = ""
    for getter in (lambda: unreal.SystemLibrary.get_project_saved_directory(),
                   lambda: os.path.join(str(unreal.Paths.project_dir()), "Saved")):
        try:
            value = getter()
        except Exception:
            value = None
        if value:
            root = str(value).rstrip("/\\")
            break
    if not root:
        root = os.path.join(os.getcwd(), "Saved")
    return os.path.join(root, "Prism", "%s_%s.json" % (op, stamp))


def _lod_rows(unreal, obj, tried):
    """(num_lods, {index: triangles})：metrics 的窗口最深到 LOD7，越界就不给数字。"""
    mtried = []
    metrics = None
    try:
        metrics = _m._measure_mesh(unreal, obj, mtried)
    except Exception:
        metrics = None
    tried.extend(mtried)
    nv, via, vt = _try_call(obj, "get_num_lods", [((), "noarg")])
    tried.extend(vt)
    rows = (metrics or {}).get("lods") or []
    per = {}
    for row in rows:
        if isinstance(row, dict) and row.get("triangles") is not None:
            per[_int(row.get("index"), len(per))] = _int(row.get("triangles"), 0)
    num = _int(nv, 0) if nv is not None else (len(rows) if rows else 0)
    return num, per, (via or ("lods:%d" % len(rows)))


def _min_lod_view(obj):
    """(当前平台覆盖字典, 命中签名, tried)：map 与逐平台并取（与只读扫描同一口径）。"""
    return read_min_lod(obj)


def _write_min_lod(obj, platform, want):
    """写每平台 Min LOD 并回读校验；写不进/读不回一律 failed，绝不假成功。

    保存由 `optutil.drive` 统一调（save_asset），回滚清单落在 opt_mesh_set_min_lod 里。
    """
    tried = []
    before, _bvia, bt = _min_lod_view(obj)
    tried.extend(bt)
    before_val = _int((before or {}).get(platform), -1)
    if before_val == want:
        return True, "no_write_needed", tried
    api = None
    for label, call in (
        ("set_minimum_lod_for_platform(%s,%d)" % (platform, want),
         lambda: obj.set_minimum_lod_for_platform(platform, want)),
    ):
        try:
            call()
            api = label
            tried.append({"sig": label, "err": ""})
            break
        except Exception as exc:
            tried.append({"sig": label, "err": "%s:%s" % (type(exc).__name__, str(exc)[:120])})
    if api is None:
        return False, None, tried
    after_map, avia, at = _min_lod_view(obj)
    tried.extend(at)
    after_val = _int((after_map or {}).get(platform), -1)
    if after_val != want:
        # 写进去了但回读不符：按失败报，附带实测值——这是最容易被假装成功的一步
        tried.append({"sig": "readback(" + str(avia) + ")",
                      "err": "mismatch got=%s want=%s before=%s" % (after_val, want, before_val)})
        return False, api, tried
    return True, api, tried


def inspect_target(unreal, obj, target, platform, want, block_nanite, min_tri, tried,
                   restore_value=None):
    """单项判定（模块级，L0 直接钉闸门语义；drive 的 inspect 闭包只负责转参）。

    `restore_value` 非 None 即回滚模式：目标值来自清单记录的原值，优化闸门一律不开。
    行 kind 语义由 `optutil.drive` 解释：change=待改/已改、no_op=已达标、
    skip=不该动（带原因）、其它=判定失败（带 tried）。
    """
    if restore_value is not None:
        cur, cur_via, ct = _min_lod_view(obj)
        tried.extend(ct)
        now = _int((cur or {}).get(platform), -1)
        row = {"platform": platform, "num_lods": None, "before": now, "after": restore_value}
        if now == restore_value:
            return dict(row, kind="no_op",
                        reason="%s 已是 %d（清单值），无需回滚（读法 %s）"
                               % (platform, restore_value, cur_via))
        return dict(row, kind="change", detail={"mode": "restore", "read_via": cur_via})

    num, per, _num_via = _lod_rows(unreal, obj, tried)
    cur, cur_via, ct = _min_lod_view(obj)
    tried.extend(ct)
    now = _int((cur or {}).get(platform), -1)
    nanite, nanite_api, nt = _n._read_nanite(obj)
    tried.extend(nt)
    tri0 = per.get(0)
    row = {"platform": platform, "num_lods": num, "nanite_enabled": nanite,
           "before": now, "triangles": tri0}
    if not num:
        return dict(row, kind="unknown", after=want,
                    reason="LOD 级数读不到（get_num_lods 不命中？）", tried=tried[-6:])
    target_lod = (num - 1) if want is None else want
    row["suggested_min_lod"] = target_lod
    if target_lod >= num:
        return dict(row, kind="skip", after=now,
                    reason="min_lod=%d >= num_lods=%d：会没级可画，拒写（要清覆盖带 min_lod=-1）"
                           % (target_lod, num))
    if num < 2 and want is None:
        return dict(row, kind="skip", after=now,
                    reason="只有一级 LOD：没有可降的级，收益为零；补 LOD 属美术侧"
                           "（scan_mesh_lod 的 no_lod_highpoly）")
    if nanite is True and block_nanite:
        return dict(row, kind="skip", after=now,
                    reason="Nanite 已开(%s)：细节由 Nanite 管，强制 Min LOD 只损画质；"
                           "确要动带 skip_nanite=False" % nanite_api)
    if tri0 is not None and tri0 < min_tri:
        return dict(row, kind="skip", after=now,
                    reason="LOD0 仅 %d 三角 < 阈值 %d：强制降级没收益（要一起改带 min_triangles=0）"
                           % (tri0, min_tri))
    if now == target_lod:
        return dict(row, kind="no_op", after=now,
                    reason="%s 平台已是 LOD%d（读法 %s）" % (platform, target_lod, cur_via))
    potential = 0
    deep = per.get(target_lod)
    if tri0 and deep is not None:
        potential = max(0, tri0 - int(deep))
    row["potential_triangles"] = potential
    detail = {"read_via": cur_via, "lod_window": len(per)}
    if not potential:
        # 报了动作但报不出削减量（建议级在实测窗口 LOD0-7 之外）：明说一句，不含糊
        detail["note"] = "reduction_unmeasured"
    return dict(row, kind="change", after=target_lod, savings_mb=0.0, detail=detail)


def write_target(obj, decision, default_platform):
    """写入并回读校验（drive 的 write 闭包只负责转参）。目标值缺失一律 failed，不猜。"""
    platform = decision.get("platform") or default_platform
    want_i = _to_int(decision.get("after"), None)
    if want_i is None:
        return False, None, [{"sig": "decision.after", "err": "missing"}]
    return _write_min_lod(obj, platform, want_i)

@register
def opt_mesh_set_min_lod(paths=None, folder="/Game", map=None, platform="ALL",
                         min_lod=None, skip_nanite=True, min_triangles=DEFAULT_MIN_TRIANGLES,
                         limit=200, offset=0, manifest=None, restore=None,
                         dry_run=True, confirm=False):
    """强制每平台 Min LOD（写·双钥）：远处/次要网格直接用最深的 LOD 级，省的是三角数与帧时间。

    这是 LOD 族唯一被探针实证「可写且回读相符」的动作面（5.3.2/5.4.4 一致）；每级
    ScreenSize 阈值在 UE Python 上读不到也写不到，所以本工具不碰阈值，也不承诺"改阈值"。

    参数与闸门：
    - platform：写哪个平台键（默认 ALL；探针实证 ALL/Windows/WindowsNoEditor 均可读写）。
    - min_lod：目标级号；留空=逐网格取最深级（num_lods-1）。-1 = 清除覆盖回到引擎默认。
      绝不会写 >= num_lods 的值（那会让网格无级可画）。
    - min_triangles：默认 20 万，与 scan_mesh_lod 同口径——低面数网格强制降级没收益，默认跳过。
    - skip_nanite：默认 true，Nanite 网格不动（细节由 Nanite 自己管，强制 Min LOD 只损画质）。
    - manifest：执行时把「原值」落一份回滚清单（默认 <Project>/Saved/Prism/minlod_*.json）；
      dry-run 不落盘。restore=<清单路径> 即按原值逐条写回（同样双钥，闸门全不开）。
    - 收益口径是**三角数（单实例单帧）**，不是 MB，不与纹理池那本账相加；实例数/可见性
      不在这里（关卡面走 analyze_instancing / list_level_actors(compose)）。
    双钥：dry_run=False 且 confirm=True 才落盘。无引擎时 executed=False + note=no_engine。
    """
    want = _to_int(min_lod, None)
    block_nanite = _u.to_bool(skip_nanite, True)
    min_tri = _int(min_triangles, DEFAULT_MIN_TRIANGLES)
    plat = str(platform or "ALL").strip() or "ALL"
    restore_path = str(restore) if restore else None

    plan = None
    if restore_path:
        plan = _read_manifest(restore_path)
        if plan is None:
            return envelope.make_err(envelope.Code.RUNTIME_ERROR,
                                     "%s restore 读不到合法清单（需 entries 数组）：%s"
                                     % (OP, restore_path))
        if plan.get("op") != OP:
            return envelope.make_err(envelope.Code.RUNTIME_ERROR,
                                     "%s restore 清单的 op=%r 不是本工具写的"
                                     % (OP, plan.get("op")))
        lookup = {}
        for entry in plan.get("entries") or []:
            if not isinstance(entry, dict) or not entry.get("package"):
                continue
            lookup[str(entry["package"])] = (str(entry.get("platform") or plat),
                                             _to_int(entry.get("before"), -1))
        pkgs = ",".join(sorted(lookup))
        if not pkgs:
            return envelope.make_err(envelope.Code.RUNTIME_ERROR,
                                     "%s restore 清单里没有条目：%s" % (OP, restore_path))
        paths = pkgs
        limit = len(lookup)

    def inspect(unreal, obj, target, tried):
        if restore_path:
            plat_i, want_i = lookup.get(target["package"], (plat, -1))
            return inspect_target(unreal, obj, target, plat_i, want, block_nanite,
                                  min_tri, tried, restore_value=want_i)
        return inspect_target(unreal, obj, target, plat, want, block_nanite,
                              min_tri, tried)

    def write(unreal, obj, decision, target):
        return write_target(obj, decision, plat)

    extra = {"mode": "restore" if restore_path else "apply",
             "platform": plat, "min_lod": want, "skip_nanite": block_nanite,
             "thresholds": {"min_triangles": min_tri},
             "restore_manifest": restore_path,
             "manifest_for_write": str(manifest) if manifest else None,
             "manifest_path": None, "rollback_available": False}
    result = _u.drive(OP, _u.MESH_TERMS, paths, folder, map if not restore_path else None,
                      limit, offset, dry_run, confirm, inspect, write, extra=extra)
    if not (isinstance(result, dict) and result.get("ok")):
        return result
    payload = result["result"] if "result" in result else result
    _finish_manifest(payload, restore_path)
    return result


def _finish_manifest(payload, restore_path):
    """执行成功 -> 落原值清单；dry-run 与 restore 模式不落（restore 本身就是回滚）。"""
    if payload.get("note") == "no_engine":
        return                              # 降级契约：note 原样留 no_engine，别改写成计划语
    if payload.get("note") and not payload.get("total") and not payload.get("executed"):
        return                              # 没碰到引擎（registry 不可用等）：保留 drive 的诚实原话
    if payload.get("dry_run") or payload.get("executed") is False:
        payload["note"] = _plan_note(payload, restore_path)
        return
    if restore_path:
        payload["note"] = _done_note(payload, restore_mode=True)
        return
    rows = []
    for row in payload.get("targets") or []:
        if row.get("kind") != "change":
            continue
        if row.get("api") is None and row.get("before") is None:
            continue
        rows.append({"package": row.get("package"),
                     "platform": row.get("platform"),
                     "before": row.get("before"), "after": row.get("after"),
                     "saved": bool(row.get("saved"))})
    payload["rollback_entries"] = len(rows)
    if not rows:
        payload["note"] = _done_note(payload, restore_mode=False)
        return
    try:
        path = _manifest_path(_unreal_or_none(), payload.get("manifest_for_write"), OP)
    except Exception as exc:
        payload["manifest_error"] = type(exc).__name__
        payload["note"] = _done_note(payload, restore_mode=False)
        return
    try:
        written = _write_json_atomic(path, {"version": MANIFEST_VERSION, "op": OP,
                                            "entries": rows})
        payload["manifest_path"] = written
        payload["rollback_available"] = True
        payload["note"] = _done_note(payload, restore_mode=False)
    except Exception as exc:
        payload["manifest_error"] = type(exc).__name__
        payload["manifest_path"] = str(path)
        payload["note"] = (_done_note(payload, restore_mode=False)
                           + " 回滚清单写入失败(%s)：原值仍在各行 before 里，可手工回滚。"
                           % type(exc).__name__)


def _plan_note(payload, restore_path):
    n = payload.get("planned") or 0
    if restore_path:
        return ("dry-run 回滚计划：将按清单写回 %d 项原值（清单 %s）。"
                "确认后带 dry_run=False 且 confirm=True 执行。" % (n, restore_path))
    pot = 0
    for row in payload.get("targets") or []:
        if row.get("kind") == "change":
            pot += int(row.get("potential_triangles") or 0)
    unmeasured = sum(1 for row in (payload.get("targets") or [])
                     if row.get("kind") == "change" and not row.get("potential_triangles"))
    tail = ("（其中 %d 项的建议级超出实测窗口 LOD0-7，未给削减数字）" % unmeasured
            if unmeasured else "")
    return ("dry-run 计划：将把 %d 个网格在 %s 平台强制到 Min LOD（合计潜在削减 %d 三角，"
            "单实例单帧口径，不是 MB%s）。已达标 %d，跳过 %d，失败 %d。"
            "确认后带 dry_run=False 且 confirm=True 执行；执行时会落一份原值清单可回滚。"
            % (n, payload.get("platform"), pot, tail, payload.get("no_op") or 0,
               len(payload.get("skipped") or []), len(payload.get("failed") or [])))


def _done_note(payload, restore_mode=False):
    changed = payload.get("changed") or 0
    if restore_mode:
        return ("已按清单回写 %d 项原值（跳过 %d，失败 %d）。复跑 scan_mesh_lod 核对。"
                % (changed, len(payload.get("skipped") or []),
                   len(payload.get("failed") or [])))
    tail = ""
    if payload.get("rollback_available"):
        tail = " 回滚清单：%s（restore=<该路径> 双钥执行即写回原值）。" % payload.get("manifest_path")
    elif payload.get("manifest_error"):
        tail = " 回滚清单未落盘（%s）。" % payload["manifest_error"]
    return ("已改 %d 项（已达标 %d，跳过 %d，失败 %d）。Min LOD 是资产属性，改动即刻生效于编辑器内"
            "预览与后续 cook；复跑 scan_mesh_lod 核对。%s"
            % (changed, payload.get("no_op") or 0, len(payload.get("skipped") or []),
               len(payload.get("failed") or []), tail))


def _unreal_or_none():
    try:
        import unreal
        return unreal
    except Exception:
        return None
