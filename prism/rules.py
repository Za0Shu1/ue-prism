"""v0.3 性能规则引擎（server 侧；DESIGN_v0.3.md）。

铁律落位：本模块在 server 进程运行（Python>=3.10：3.11+ 用 tomllib，3.10 用 tomli 兜底），离线规则只读磁盘与任务档案；
bridge 通道规则经总线批量取数（describe_many/get_asset_metrics/list_level_actors），
绝不 import unreal。桥不可用（心跳陈旧/探测失败）时在线规则整体 skip，报告仍出离线半份。

- 规则即数据：内置 profile 阈值（pc/console/mobile）+ 可选工程根 prism.toml 覆盖。
- 输出 findings：{rule_id, severity(error|warn), subject, evidence, threshold, advice}。
- 列表纪律：severity 降序聚合，cap/total/truncated 携带；单规则异常记 skipped 不裸崩。
"""
from __future__ import annotations

import copy
import glob
import os
import re
try:  # tomllib 仅 3.11+；3.10 用等价的 tomli（见 pyproject 条件依赖）
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

from . import attributelog, bus, folderscan, tasks

PROFILES = {
    "pc": {"asset_size_mb": {"warn": 20.0, "error": 100.0},
           "texture_px": {"warn": 2048, "error": 4096},
           "mesh_tri": {"warn": 200000, "error": 1000000, "mat_slots_warn": 8},
           "wps_external_actors": {"warn": 300, "error": 1000},
           "scene_light_dup": {"max_dup": 1}},
    "console": {"asset_size_mb": {"warn": 10.0, "error": 50.0},
                "texture_px": {"warn": 2048, "error": 4096},
                "mesh_tri": {"warn": 150000, "error": 500000, "mat_slots_warn": 4},
                "wps_external_actors": {"warn": 300, "error": 1000},
                "scene_light_dup": {"max_dup": 1}},
    "mobile": {"asset_size_mb": {"warn": 5.0, "error": 20.0},
               "texture_px": {"warn": 1024, "error": 2048},
               "mesh_tri": {"warn": 50000, "error": 150000, "mat_slots_warn": 4},
               "wps_external_actors": {"warn": 200, "error": 600},
               "scene_light_dup": {"max_dup": 1}},
}
DEFAULT_CAP = 50
DEFAULT_RECENT_TASKS = 3
DEFAULT_SAMPLE = 80
METRIC_CLASSES = {"Texture2D", "TextureCube", "VirtualTexture2D", "StaticMesh"}

# 真机校准发现：cook succeeded 但依赖缺失仅报 Warning 并静默丢弃（DESIGN_v0.2 PR-4 ③）
_MAPS_RE = re.compile(r'\+maps="([^"]+)"')
_COOKED_TOTAL_RE = re.compile(r"Cooked packages \d+ Packages Remain \d+ Total (\d+)")
_SPLIT_RE = re.compile(r"Splitting Package (/Game/[^\s]+)")

DROP_MARKERS = (
    "does not exist, will not scan",
    "Unknown actor base class",
    "could not be found",
)


def load_profile(project_dir, target=None):
    cfg = {}
    toml_path = os.path.join(project_dir or "", "prism.toml")
    if os.path.isfile(toml_path):
        with open(toml_path, "rb") as f:
            cfg = tomllib.load(f)
    target = target or (cfg.get("profile") or {}).get("target") or "pc"
    if target not in PROFILES:
        raise ValueError("unknown perf target %r (pc|console|mobile)" % target)
    profile = copy.deepcopy(PROFILES[target])
    for rid, vals in (cfg.get("rules") or {}).items():
        sec = profile.setdefault(rid, {})
        if isinstance(vals, dict):
            sec.update(vals)
    return profile, target


def _bridge_status(bus_dir):
    """心跳优先；无心跳文件时发一条空批量做 3s 轻探测（旧桥 UNKNOWN_FN 即判离线）。"""
    age = bus.heartbeat_age(bus_dir)
    if age is not None:
        return age <= bus.HB_STALE_SECONDS
    probe = bus.BusClient(bus_dir, timeout=3).call("describe_many", {"paths": []})
    return bool(probe.get("ok"))


def _ensure_scan(ctx):
    if "scan" not in ctx:
        ss = ctx.get("sample_size")
        limit = 2000
        if isinstance(ss, int) and ss > limit:
            limit = ss
        ctx["scan"] = folderscan.scan_folder_assets(ctx["project_dir"], folder=ctx["scope"], limit=limit)
    return ctx["scan"]


def _sample_assets(assets, sample_size):
    """bridge 规则候选：按 size 降序取前 sample_size 个大资产；sample_size<=0 表示全量（取扫描宇宙）。"""
    try:
        n = int(sample_size)
    except (TypeError, ValueError):
        n = DEFAULT_SAMPLE
    return list(assets) if n <= 0 else list(assets)[:n]


def _ensure_classes(ctx):
    if "classes" not in ctx:
        classes = {}
        scan = _ensure_scan(ctx)
        cands = [a["asset_path"] for a in _sample_assets(scan["assets"], ctx.get("sample_size", DEFAULT_SAMPLE))]
        if cands:
            d = bus.BusClient(ctx["bus_dir"], timeout=60).call("describe_many", {"paths": cands, "limit": len(cands)})
            if d.get("ok"):
                for it in d["result"]["items"]:
                    pkg = it.get("package_name") or it.get("query")
                    if pkg and it.get("class"):
                        classes[pkg] = it["class"]
        ctx["classes"] = classes
    return ctx["classes"]


def _ensure_metrics(ctx):
    if "metrics" not in ctx:
        out = {}
        want = [p for p, c in _ensure_classes(ctx).items() if c in METRIC_CLASSES]
        want.sort()
        client = bus.BusClient(ctx["bus_dir"], timeout=120)
        for i in range(0, len(want), 30):
            g = client.call("get_asset_metrics", {"asset_paths": want[i:i + 30], "max_assets": 30})
            if g.get("ok"):
                for it in g["result"]["items"]:
                    key = it.get("package_name") or it.get("query")
                    if key:
                        out[key] = it
        ctx["metrics"] = out
    return ctx["metrics"]


def _ensure_actors(ctx):
    if "actors" not in ctx:
        r = bus.BusClient(ctx["bus_dir"], timeout=60).call("list_level_actors", {"limit": 2000})
        ctx["actors"] = r["result"]["actors"] if r.get("ok") else None
    return ctx["actors"]


def _cook_records(ctx):
    """cook/package 档案窗口：滤 kind -> created_ts 倒序 -> 取最近 recent_tasks 个（0/None=全部档案）。"""
    if "cook_recs" not in ctx:
        recs = [r for r in tasks.list_records(ctx["bus_dir"])
                if r.get("kind") in ("cook", "package")
                and tasks.derive_state(ctx["bus_dir"], r)[0] == tasks.STATUS_SUCCEEDED]
        recs.sort(key=lambda r: r.get("created_ts") or 0, reverse=True)
        k = ctx.get("recent_tasks") or 0
        ctx["cook_recs"] = recs[:k] if k > 0 else recs
        ctx["cook_recs_total"] = len(recs)
    return ctx["cook_recs"]


def _rule_asset_size_top(ctx):
    th = ctx["profile"]["asset_size_mb"]
    out = []
    for a in _ensure_scan(ctx)["assets"]:
        mb = a.get("size_mb") or 0.0
        if mb >= float(th["error"]):
            sev, limit = "error", float(th["error"])
        elif mb >= float(th["warn"]):
            sev, limit = "warn", float(th["warn"])
        else:
            continue
        out.append({"rule_id": "asset_size_top", "severity": sev, "subject": a["asset_path"],
                    "evidence": {"size_mb": mb}, "threshold": limit,
                    "advice": "check resolution/complexity; consider streaming or compression"})
    return out


def _package_on_disk(project_dir, pkg):
    """/Game 包现状复核：Content/<路径>.uasset/.umap 存在 = 依赖已修复（修复后待重 cook）。"""
    rel = str(pkg)[len("/Game"):].strip("/").split("/")
    if not rel or not rel[0]:
        return False
    base = os.path.join(str(project_dir), "Content", *rel)
    return os.path.isfile(base + ".uasset") or os.path.isfile(base + ".umap")


def _rule_cook_drop(ctx):
    out = []
    reported = set()  # 窗口新→旧遍历：同一 subject 只归最新档案，更新的成功 cook 会让陈旧证据自动过期
    for rec in _cook_records(ctx):
        status, _ = tasks.derive_state(ctx["bus_dir"], rec)
        if status != tasks.STATUS_SUCCEEDED:
            continue
        log = rec.get("log_path") or ""
        try:
            with open(log, "r", encoding="utf-8-sig", errors="replace") as f:
                lines = f.readlines()
        except OSError:
            continue
        hits = 0
        seen = set()  # 同一任务内按资产去重：关服时 LogInit 会回显 Warning，防双计
        for i, raw in enumerate(lines):
            if hits >= 10:
                break
            if any(mk in raw for mk in DROP_MARKERS):
                found = [m.split(".", 1)[0] for m in attributelog.GAME_RE.findall(raw)]
                key = found[0] if found else rec["task_id"]
                if key in seen or key in reported:
                    continue
                if key.startswith("/Game") and _package_on_disk(ctx["project_dir"], key):
                    seen.add(key)
                    reported.add(key)
                    continue  # 现状复核：包已在磁盘(修复后未重 cook)，该警告已不代表当前状态
                seen.add(key)
                reported.add(key)
                hits += 1
                out.append({"rule_id": "cook_drop", "severity": "error",
                            "subject": found[0] if found else rec["task_id"],
                            "evidence": {"task_id": rec["task_id"], "line_no": i + 1,
                                         "task_created_ts": rec.get("created_ts"),
                                         "disk_check": ("package_absent" if key.startswith("/Game") else "unverifiable"),
                                         "excerpt": raw.strip()[:200]},
                            "threshold": None,
                            "advice": "cook succeeded but content was dropped (and package is still absent on disk); "
                                      "restore/fix the missing dependency"})
    return out


def _rule_wps_external_actors(ctx):
    th = ctx["profile"]["wps_external_actors"]
    count = sum(1 for a in _ensure_scan(ctx)["assets"] if a["asset_path"].startswith("/Game/__ExternalActors__/"))
    sev = "error" if count >= int(th["error"]) else ("warn" if count >= int(th["warn"]) else None)
    if not sev:
        return []
    return [{"rule_id": "wps_external_actors", "severity": sev, "subject": "/Game/__ExternalActors__",
             "evidence": {"count": count}, "threshold": int(th[sev]),
             "advice": "many external actor packages; review WP grid/hitch and merge static where possible"}]


def _rule_texture_size(ctx):
    th = ctx["profile"]["texture_px"]
    out = []
    for pkg, it in sorted(_ensure_metrics(ctx).items()):
        m = it.get("metrics") or {}
        # 按"源尺寸"判（编辑器冷热态下 width 直读可能是占位或限幅后有效值）
        px = int(m.get("source_width") or m.get("width") or 0)
        if px <= 0:
            continue
        if px >= int(th["error"]):
            sev, limit = "error", int(th["error"])
        elif px >= int(th["warn"]):
            sev, limit = "warn", int(th["warn"])
        else:
            continue
        # 5.4 真机校准：源尺寸大但运行时已被 MaxSize 限到很小 -> 非显存问题，降 warn
        est_mb = (m.get("est_runtime_bytes") or 0) / 1048576.0
        capped_small = bool(m.get("max_size")) and 0 < est_mb <= 1.0
        if sev == "error" and capped_small:
            sev, limit = "warn", int(th["warn"])
        # 运行时证据：常驻内存可能是资源未上传的占位值，以派生估算为准
        evidence = {"width": px, "height": m.get("height"),
                    "resident_bytes": m.get("memory_bytes"),
                    "est_runtime_mb": round(est_mb, 3),
                    "max_size": m.get("max_size"), "virtual_texture": m.get("virtual_texture")}
        evidence = {k: v for k, v in evidence.items() if v is not None}
        if m.get("size_derived"):
            evidence["size_derived"] = True
        if m.get("effective_width"):
            evidence["effective_width"] = m["effective_width"]
        if capped_small:
            advice = ("source is large but runtime already capped by MaxSize (est %.2fMB); "
                      "cost is disk/cook only - consider re-import at target res" % est_mb)
        else:
            advice = "downscale, virtualize, or use texture LOD bias"
        out.append({"rule_id": "texture_size", "severity": sev, "subject": pkg,
                    "evidence": evidence, "threshold": limit, "advice": advice})
    return out

def _rule_mesh_tri(ctx):
    th = ctx["profile"]["mesh_tri"]
    out = []
    for pkg, it in sorted(_ensure_metrics(ctx).items()):
        m = it.get("metrics") or {}
        lods = m.get("lods") or []
        if not lods:
            continue
        top = max(int(l.get("triangles") or 0) for l in lods)
        if top <= 0:
            continue
        if top >= int(th["error"]):
            sev, limit = "error", int(th["error"])
        elif top >= int(th["warn"]):
            sev, limit = "warn", int(th["warn"])
        else:
            continue
        evidence = {"triangles_lod0": top, "lod_count": m.get("lod_count")}
        if m.get("material_slots"):
            evidence["material_slots"] = int(m["material_slots"])
        if m.get("collision_triangles"):
            evidence["collision_triangles"] = int(m["collision_triangles"])
        advice = "reduce complexity / add LODs"
        if (m.get("lod_count") or 0) <= 1:
            advice = "single-LOD high-poly mesh: author LODs first"
        if m.get("material_slots") and int(m["material_slots"]) >= int(th.get("mat_slots_warn", 8)):
            advice += " (many material slots split draw calls)"
        out.append({"rule_id": "mesh_tri", "severity": sev, "subject": pkg,
                    "evidence": evidence, "threshold": limit, "advice": advice})
    return out


def _rule_scene_light_dup(ctx):
    actors = _ensure_actors(ctx)
    if actors is None:
        return []
    th = ctx["profile"]["scene_light_dup"]
    counts = {}
    for a in actors:
        if a.get("class") in ("DirectionalLight", "SkyLight"):
            counts[a["class"]] = counts.get(a["class"], 0) + 1
    dups = {k: v for k, v in counts.items() if v > int(th["max_dup"])}
    if not dups:
        return []
    return [{"rule_id": "scene_light_dup", "severity": "warn",
             "subject": ", ".join("%s x%d" % (k, v) for k, v in sorted(dups.items())),
             "evidence": {"counts": dups, "actors_total": len(actors)}, "threshold": int(th["max_dup"]),
             "advice": "duplicate directional/skylights cost double lighting passes; keep one of each"}]


def _map_cooked_on_disk(project_dir, map_key):
    """增量 cook 全保留(no-op)时日志不会有任何该 map 的逐包行——用磁盘 cook 产物做第二证据源。

    Saved/Cooked/<平台>/<工程>/Content/<地图完整子路径>.umap 存在 = 曾被成功 cook（增量 no-op 只是没重煮它）。
    """
    mk = str(map_key).replace("\\", "/")
    if mk.startswith("/Game/"):
        rel = mk[len("/Game/"):]
    else:
        rel = mk.strip("/")
    if not rel:
        return False
    stem = rel.rpartition("/")[2]
    head = os.path.join(str(project_dir), "Saved", "Cooked", "*", "*", "Content")
    # cooked 地图按 /Game 下完整层级镜像落盘（真机通常不在 Content 根），精确匹配子路径
    if glob.glob(os.path.join(head, *rel.split("/")) + ".umap"):
        return True
    # 兜底：map_key 只给短名时按任意深度同名匹配
    return bool(glob.glob(os.path.join(head, "**", stem + ".umap"), recursive=True))


def _rule_cook_empty_maps(ctx):
    """校准发现②规则化：+maps 里的地图名从未被 cook（静默忽略），或整轮 0 包。"""
    out = []
    reported_maps = set()
    for rec in _cook_records(ctx):
        status, _ = tasks.derive_state(ctx["bus_dir"], rec)
        if status != tasks.STATUS_SUCCEEDED:
            continue
        log = rec.get("log_path") or ""
        try:
            with open(log, "r", encoding="utf-8-sig", errors="replace") as f:
                text = f.read()
        except OSError:
            continue
        # 只认实际 cook 证据行；命令回显行（Parsing command line）会原样带上 +maps，
        # 用它判"煮过"会把错误 map 名误判为已 cook（真机踩坑修正）。
        blob = "\n".join(ln for ln in text.splitlines()
                        if "Parsing command line" not in ln
                        and ("LogCook" in ln or "LogSavePackage" in ln or "Splitting Package" in ln))
        totals = _COOKED_TOTAL_RE.findall(text)
        for mp in _MAPS_RE.findall(rec.get("command") or ""):
            key = mp if mp.startswith("/Game/") else "/Game/" + mp
            cooked = key in blob
            if not cooked and _map_cooked_on_disk(ctx["project_dir"], key):
                cooked = True  # 增量保留 no-op：日志无逐包证据，但磁盘产物证明煮过（如 -iterate 全 up-to-date）
            if not cooked and key not in reported_maps:
                reported_maps.add(key)
                out.append({"rule_id": "cook_empty_maps", "severity": "error", "subject": key,
                            "evidence": {"task_id": rec["task_id"], "requested_map": mp,
                                         "check": "map_not_cooked",
                                         "disk_check": "cooked_artifact_absent",
                                         "task_created_ts": rec.get("created_ts")},
                            "threshold": None,
                            "advice": "requested map never cooked (no log evidence and no cooked .umap on disk; "
                                      "UE silently ignores bad +maps) - verify name via ping loaded_maps"})
        if totals and int(totals[-1]) == 0:
            out.append({"rule_id": "cook_empty_maps", "severity": "warn", "subject": rec["task_id"],
                        "evidence": {"cooked_total": 0, "check": "zero_package_cook",
                                     "task_created_ts": rec.get("created_ts")},
                        "threshold": None,
                        "advice": "cook reported success with 0 cooked packages - likely stale no-op; rerun with iterate=False"})
    return out


def apply_runtime_verdicts(findings, metrics):
    """bridge 在线时用运行时度量给 asset_size_top 消误报：假大(disk_only_bloat)降档 error->warn。

    只降 disk_only_bloat 这一种有双证据(源盘 vs 运行时)的结论；
    unknown/缺数据一律不动——宁可保守报大，不假装 certainty。返回降档条数。
    """
    changed = 0
    if not metrics:
        return 0
    for f in findings:
        if f.get("rule_id") != "asset_size_top":
            continue
        it = metrics.get(f.get("subject")) or {}
        rv = (it.get("metrics") or {}).get("runtime_verdict") or {}
        if rv.get("verdict") != "disk_only_bloat":
            continue
        ev = f.setdefault("evidence", {})
        ev["runtime_verdict"] = "disk_only_bloat"
        ev["runtime_mb"] = rv.get("runtime_mb")
        ev["disk_mb"] = rv.get("disk_mb")
        if f.get("severity") == "error":
            f["severity"] = "warn"
        f["advice"] = ("source-disk bloat only (runtime %.2fMB vs disk %.1fMB; MaxSize/compression "
                       "caps runtime) - re-import at target res; do not chase cooked-size" %
                       (rv.get("runtime_mb") or 0.0, rv.get("disk_mb") or 0.0))
        changed += 1
    return changed


RULES = (
    {"id": "asset_size_top", "cfg": "asset_size_mb", "channel": "offline", "doc": "单资产磁盘大小超阈值", "run": _rule_asset_size_top},
    {"id": "cook_drop", "cfg": None, "channel": "offline", "doc": "cook succeeded 但日志含静默丢弃依赖告警（校准发现③）", "run": _rule_cook_drop},
    {"id": "cook_empty_maps", "cfg": None, "channel": "offline", "doc": "请求的 map 从未被 cook / 0 包空 cook（校准发现②）", "run": _rule_cook_empty_maps},
    {"id": "wps_external_actors", "cfg": "wps_external_actors", "channel": "offline", "doc": "WP 外部 actor 包数量超阈值（真机案例 148 包）", "run": _rule_wps_external_actors},
    {"id": "texture_size", "cfg": "texture_px", "channel": "bridge", "doc": "纹理尺寸超阈值（按 sample_size 抽样度量最大资产）", "run": _rule_texture_size},
    {"id": "mesh_tri", "cfg": "mesh_tri", "channel": "bridge", "doc": "网格 LOD0 三角数超阈值/单 LOD 高模（附材质槽/碰撞维度）", "run": _rule_mesh_tri},
    {"id": "scene_light_dup", "cfg": "scene_light_dup", "channel": "bridge", "doc": "DirectionalLight/SkyLight 重复放置", "run": _rule_scene_light_dup},
)


# ---- ROI 注解（发现 -> 工单：给每条 finding 粗量化收益 + 改动成本档）----
# 原则：宁可粗档，不造伪精确。体量类给 MB，渲染/正确性类给可数代理指标或标注 correctness。
_EFFORT_BY_RULE = {
    "asset_size_top": "mid", "texture_size": "mid", "mesh_tri": "high",
    "wps_external_actors": "mid", "scene_light_dup": "low",
    "cook_drop": "high", "cook_empty_maps": "high",
}


def _roi_of(f):
    rid = f.get("rule_id")
    ev = f.get("evidence") or {}
    effort = _EFFORT_BY_RULE.get(rid, "mid")
    unit, benefit, rationale = "correctness", None, ""
    if rid == "asset_size_top":
        mb = ev.get("size_mb")
        if isinstance(mb, (int, float)):
            unit, benefit = "memory_or_disk_mb", round(float(mb), 2)
        if ev.get("runtime_verdict") == "disk_only_bloat":
            effort, rationale = "low", "源盘膨胀但运行时被 MaxSize/压缩限死，收 MaxSize/重压缩即省源盘，成本极低"
        else:
            rationale = "真实占用，需降分辨率/流送/压缩，成本视资产而定"
    elif rid == "texture_size":
        mb = ev.get("size_mb")
        if isinstance(mb, (int, float)):
            unit, benefit = "memory_or_disk_mb", round(float(mb), 2)
        else:
            unit = "texture_px"
            benefit = ev.get("width")
        effort = "low" if ev.get("runtime_verdict") == "disk_only_bloat" else effort
        rationale = "降分辨率/虚拟化/LOD 偏差"
    elif rid == "mesh_tri":
        unit = "triangles_lod0"
        benefit = ev.get("triangles_lod0")
        rationale = "重做 LOD/减面，牵动美术资产，成本高"
    elif rid == "wps_external_actors":
        unit = "external_actor_packages"
        benefit = ev.get("count")
        rationale = "WP 外部包过多影响加载/hitch，合并静态或调网格，成本中"
    elif rid == "scene_light_dup":
        counts = ev.get("counts") or {}
        extra = 0
        for _k, v in counts.items():
            if isinstance(v, (int, float)):
                extra += max(0, int(v) - 1)
        unit, benefit = "extra_light_passes", extra
        rationale = "删重复方向光/天光，成本低"
    elif rid in ("cook_drop", "cook_empty_maps"):
        rationale = "cook 正确性问题（内容被丢弃/地图没烘进包），须修管线，优先但不是体量收益"
    else:
        rationale = "见 advice"
    return {"unit": unit, "benefit": benefit, "effort": effort, "rationale": rationale}


def annotate_roi(findings):
    """原地给每条 finding 加 roi 字段，并返回顶层 roi_summary（体量收益合计 + 按成本分档 + 正确性计数）。"""
    summary = {"total_benefit_mb": 0.0, "by_effort": {"low": 0, "mid": 0, "high": 0},
               "correctness": 0, "actionable_size": 0, "top_size_targets": []}
    for f in findings:
        roi = _roi_of(f)
        f["roi"] = roi
        summary["by_effort"][roi["effort"]] = summary["by_effort"].get(roi["effort"], 0) + 1
        if roi["unit"] == "memory_or_disk_mb" and isinstance(roi["benefit"], (int, float)):
            summary["total_benefit_mb"] = round(summary["total_benefit_mb"] + roi["benefit"], 2)
            summary["actionable_size"] += 1
            summary["top_size_targets"].append({"subject": f.get("subject"), "benefit_mb": roi["benefit"],
                                                "effort": roi["effort"], "severity": f.get("severity")})
        elif roi["unit"] == "correctness":
            summary["correctness"] += 1
    summary["top_size_targets"].sort(key=lambda x: (-x["benefit_mb"], x["effort"], x["subject"]))
    summary["top_size_targets"] = summary["top_size_targets"][:20]
    return summary


# ---- vs-上次回归 diff（快照落 <bus_dir>/reports/，读上一份 latest.json 比较）----
import json as _json
import time as _time


def _reports_dir(bus_dir):
    return os.path.join(str(bus_dir), "reports")


def _snap_stamp(stamp=None):
    if stamp:
        return str(stamp)
    return "%s_%d" % (_time.strftime("%Y%m%dT%H%M%SZ", _time.gmtime()),
                      int(_time.time() * 1000) % 1000)


def _finding_key(f):
    return "%s::%s" % (f.get("rule_id"), f.get("subject"))


def _fingerprints(findings):
    out = {}
    for f in findings:
        out[_finding_key(f)] = f.get("severity")
    return out


def _prev_snapshot(reports_dir):
    lp = os.path.join(reports_dir, "latest.json")
    try:
        with open(lp, "r", encoding="utf-8") as fh:
            return _json.load(fh)
    except Exception:
        return None


def _write_snapshot(reports_dir, snap):
    try:
        os.makedirs(reports_dir, exist_ok=True)
        fn = "report_%s.json" % snap["stamp"]
        with open(os.path.join(reports_dir, fn), "w", encoding="utf-8") as fh:
            _json.dump(snap, fh, ensure_ascii=False)
        with open(os.path.join(reports_dir, "latest.json"), "w", encoding="utf-8") as fh:
            _json.dump(snap, fh, ensure_ascii=False)
        return fn
    except Exception:
        return None


def _split_key(k):
    rid, _sep, subj = k.partition("::")
    return {"rule_id": rid, "subject": subj}


def _d(a, b):
    try:
        return round(float(a) - float(b), 3)
    except (TypeError, ValueError):
        return None


def _compute_diff(prev, findings, roi_summary, summary):
    if not prev:
        return {"available": False,
                "note": "无上次快照(首次运行或历史被清)：本次已作为基线存下，下次起可 diff。"}
    prev_fp = prev.get("fingerprints") or {}
    cur_fp = _fingerprints(findings)
    prev_keys, cur_keys = set(prev_fp), set(cur_fp)
    added = []
    for k in sorted(cur_keys - prev_keys):
        d = _split_key(k); d["severity"] = cur_fp[k]; added.append(d)
    removed = []
    for k in sorted(prev_keys - cur_keys):
        d = _split_key(k); d["severity"] = prev_fp[k]; removed.append(d)
    changed = []
    for k in sorted(cur_keys & prev_keys):
        if prev_fp[k] != cur_fp[k]:
            d = _split_key(k); d.update({"from": prev_fp[k], "to": cur_fp[k]}); changed.append(d)
    prev_sum = prev.get("summary") or {}
    prev_roi = prev.get("roi_summary") or {}
    return {
        "available": True, "vs_stamp": prev.get("stamp"), "vs_ts": prev.get("ts"),
        "added": added[:50], "added_count": len(added),
        "removed": removed[:50], "removed_count": len(removed),
        "severity_changed": changed[:50], "severity_changed_count": len(changed),
        "error_delta": _d(summary.get("error"), prev_sum.get("error")),
        "warn_delta": _d(summary.get("warn"), prev_sum.get("warn")),
        "total_benefit_mb_delta": _d(roi_summary.get("total_benefit_mb"), prev_roi.get("total_benefit_mb")),
    }


def run_report(project_dir, bus_dir, scope="/Game", target=None, cap=DEFAULT_CAP,
               recent_tasks=DEFAULT_RECENT_TASKS, sample_size=DEFAULT_SAMPLE,
               snapshot=True, stamp=None):
    profile, used_target = load_profile(project_dir, target)
    # scope 必须映射到 Content 下真实存在的目录：无效 scope 直接报错，拒绝静默跳规则出"看似干净"的残缺报告
    scope = str(scope or "/Game").strip() or "/Game"
    scope_root = folderscan._disk_root(project_dir, scope)
    if not os.path.isdir(scope_root):
        raise ValueError("scope 不存在: %r -> %s（应为 /Game 下现有文件夹，可省略 /Game 前缀）" % (scope, scope_root))
    try:
        recent_tasks = int(recent_tasks)
    except (TypeError, ValueError):
        raise ValueError("recent_tasks 需为整数（0=扫描全部历史档案），got %r" % (recent_tasks,))
    try:
        sample_size = int(sample_size)
    except (TypeError, ValueError):
        raise ValueError("sample_size 需为整数（0=全量, >0=测最大的前 N 个大资产），got %r" % (sample_size,))
    try:
        cap = int(cap)
    except (TypeError, ValueError):
        raise ValueError("cap 需为整数（<=0=不截断，全部返回），got %r" % (cap,))
    ctx = {"project_dir": project_dir, "bus_dir": bus_dir, "scope": scope, "profile": profile,
           "recent_tasks": recent_tasks, "sample_size": sample_size}
    findings, skipped = [], []
    try:
        bridge_online = _bridge_status(bus_dir)
    except Exception as e:
        bridge_online = False
        skipped.append("bridge_probe: %s" % str(e)[:80])
    for rule in RULES:
        if rule["channel"] == "bridge" and not bridge_online:
            skipped.append("%s: bridge offline" % rule["id"])
            continue
        try:
            findings.extend(rule["run"](ctx))
        except FileNotFoundError as e:
            skipped.append("%s: %s" % (rule["id"], e))
        except Exception as e:
            skipped.append("%s: %s" % (rule["id"], str(e)[:120]))
    runtime_downgraded = 0
    if bridge_online:
        try:
            runtime_downgraded = apply_runtime_verdicts(findings, _ensure_metrics(ctx))
        except Exception as e:
            skipped.append("asset_size_top runtime downgrade: %s" % str(e)[:120])
    window_ids = [r["task_id"] for r in _cook_records(ctx)]
    rank = {"error": 0, "warn": 1}
    findings.sort(key=lambda x: (rank.get(x["severity"], 9), x["rule_id"], x["subject"]))  # 稳定序：diff 友好
    roi_summary = annotate_roi(findings)
    total = len(findings)
    summary = {"error": sum(1 for x in findings if x["severity"] == "error"),
               "warn": sum(1 for x in findings if x["severity"] == "warn"),
               "skipped_rules": skipped}
    cur_fp = _fingerprints(findings)
    if bool(snapshot):
        _rd = _reports_dir(bus_dir)
        prev = _prev_snapshot(_rd)
        diff = _compute_diff(prev, findings, roi_summary, summary)
        snap = {"stamp": _snap_stamp(stamp), "ts": _time.time(),
                "profile": used_target, "scope": scope,
                "summary": {"error": summary["error"], "warn": summary["warn"]},
                "roi_summary": {"total_benefit_mb": roi_summary["total_benefit_mb"],
                                "by_effort": roi_summary["by_effort"],
                                "correctness": roi_summary["correctness"],
                                "actionable_size": roi_summary["actionable_size"]},
                "fingerprints": cur_fp}
        snap_saved = _write_snapshot(_rd, snap)
    else:
        diff = {"available": False, "note": "snapshot 关闭(snapshot=False)，无法做 vs-上次回归对比。"}
        snap_saved = None
    returned = findings if cap <= 0 else findings[:cap]
    returned_errors = sum(1 for x in returned if x["severity"] == "error")
    scan_meta = ctx.get("scan") or {}
    universe = len(scan_meta.get("assets", []))
    scan_trunc = bool(scan_meta.get("truncated"))
    measured = universe if sample_size <= 0 else min(sample_size, universe)
    exhaustive = (not scan_trunc) and (sample_size <= 0 or sample_size >= universe)
    if not bridge_online:
        note = "offline half-report (bridge rules skipped)"
    elif exhaustive:
        note = "bridge rules measured all %d scanned assets (exhaustive within scan universe)" % universe
    else:
        note = ("bridge rules measure top-%d of %d scanned assets (sampled; raise sample_size or narrow scope to widen)"
                % (sample_size, universe))
    return {
        "profile": used_target,
        "scope": scope,
        "bridge": "online" if bridge_online else "offline",
        "engine": None,
        "note": note,
        "summary": summary,
        "roi_summary": roi_summary,
        "diff": diff,
        "snapshot_saved": snap_saved,
        "runtime_size_downgraded": runtime_downgraded,
        "sampling": {"sample_size": sample_size, "scan_universe": universe,
                     "scan_universe_truncated": scan_trunc, "measured": measured,
                     "exhaustive": exhaustive},
        "cook_archive": {"recent_tasks": recent_tasks,
                         "window_task_ids": window_ids,
                         "archive_total": ctx.get("cook_recs_total", len(window_ids))},
        "findings": returned,
        "total": total,
        "truncated": total > len(returned),
        "cap": cap,
        "errors_hidden": max(0, summary["error"] - returned_errors),
    }


def list_rules(target=None, project_dir=""):
    profile, used_target = load_profile(project_dir, target)
    return {"target": used_target,
            "rules": [{"id": r["id"], "channel": r["channel"], "doc": r["doc"],
                       "thresholds": profile.get(r["cfg"]) if r["cfg"] else None}
                      for r in RULES],
            "total": len(RULES), "truncated": False, "cap": len(RULES)}
