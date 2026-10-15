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

from . import attributelog, bus, folderscan, logscan, streaming, tasks

PROFILES = {
    "pc": {"asset_size_mb": {"warn": 20.0, "error": 100.0},
           "texture_px": {"warn": 2048, "error": 4096},
           "mesh_tri": {"warn": 200000, "error": 1000000, "mat_slots_warn": 8},
           "texture_streaming": {"min_demand_mb": 32.0, "tight_pct": 80.0},
           "mip_clamp": {"min_savings_mb": 4.0, "error_savings_mb": 16.0},
           "uncompressed_texture": {"min_mb": 1.0, "error_mb": 16.0},
           "nanite_translucent": {"min_count": 1, "log_tail": 60000},
           "batching": {"min_group_actors": 5, "min_collapsible": 100, "warn_share_pct": 50.0},
           "wps_external_actors": {"warn": 300, "error": 1000},
           "scene_light_dup": {"max_dup": 1}},
    "console": {"asset_size_mb": {"warn": 10.0, "error": 50.0},
                "texture_px": {"warn": 2048, "error": 4096},
                "mesh_tri": {"warn": 150000, "error": 500000, "mat_slots_warn": 4},
           "texture_streaming": {"min_demand_mb": 32.0, "tight_pct": 80.0},
           "mip_clamp": {"min_savings_mb": 4.0, "error_savings_mb": 16.0},
           "uncompressed_texture": {"min_mb": 1.0, "error_mb": 16.0},
           "nanite_translucent": {"min_count": 1, "log_tail": 60000},
           "batching": {"min_group_actors": 5, "min_collapsible": 100, "warn_share_pct": 50.0},
                "wps_external_actors": {"warn": 300, "error": 1000},
                "scene_light_dup": {"max_dup": 1}},
    "mobile": {"asset_size_mb": {"warn": 5.0, "error": 20.0},
               "texture_px": {"warn": 1024, "error": 2048},
               "mesh_tri": {"warn": 50000, "error": 150000, "mat_slots_warn": 4},
           "texture_streaming": {"min_demand_mb": 16.0, "tight_pct": 85.0},
           "mip_clamp": {"min_savings_mb": 2.0, "error_savings_mb": 8.0},
           "uncompressed_texture": {"min_mb": 0.5, "error_mb": 8.0},
           "nanite_translucent": {"min_count": 1, "log_tail": 60000},
           "batching": {"min_group_actors": 4, "min_collapsible": 60, "warn_share_pct": 60.0},
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
    """一次总线往返同时取 actor 清单与场景构成（compose=True 的 composition 覆盖全关卡，
    不受 actors 的 limit 截断影响——合批判定因此看到的是完整关卡）。"""
    if "actors" not in ctx:
        r = bus.BusClient(ctx["bus_dir"], timeout=120).call(
            "list_level_actors", {"limit": 2000, "compose": True, "composition_cap": 20})
        res = r["result"] if r.get("ok") else None
        ctx["actors"] = res["actors"] if res else None
        ctx["composition"] = res.get("composition") if res else None
    return ctx["actors"]


def _ensure_composition(ctx):
    if "composition" not in ctx:
        _ensure_actors(ctx)
    return ctx.get("composition")


def _ensure_pool_entries(ctx):
    """把 metrics 的纹理条目换成流送池条目（口径与 scan_texture_streaming 同源）。"""
    if "pool_entries" not in ctx:
        th_px = ctx["profile"]["texture_px"]
        rows = []
        for pkg, it in sorted(_ensure_metrics(ctx).items()):
            m = it.get("metrics")
            cls = it.get("class")
            if not isinstance(m, dict) or not cls:
                continue
            if "texture" not in str(cls).lower():
                continue
            entry = streaming.texture_pool_entry(m, cls, it.get("disk_bytes"),
                                                 px_error=int(th_px["error"]),
                                                 px_warn=int(th_px["warn"]))
            entry["package"] = pkg
            rows.append(entry)
        ctx["pool_entries"] = rows
    return ctx["pool_entries"]


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
        est_mb = streaming.runtime_ledger(m)["runtime_bytes"] / 1048576.0
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


# ---- v1.5 优化族规则（v1.5 Feature 1/2/3；口径与 opt_* 工具同源 prism/streaming.py）----

# 真冲突短语。5.3 真实工程（ProjectA，4623 条同源）实测文本：
#   LogStaticMesh: Warning: Invalid material [M_X] used on Nanite static mesh [SM_Y].
#   Only opaque or masked blend modes are currently supported, [BLEND_Translucent]
#   blend mode is not supported with Nanite enabled.
# 5.4/更新的写法用 "... blending mode of 'Translucent' which isn't supported by Nanite mesh ..."。
_NANITE_TRANSLUCENT_PHRASES = ("used on nanite static mesh", "supported by nanite",
                               "only opaque or masked", "invalid material")
_NANITE_TRANSLUCENT_MARKERS = ("translucent", "modulate", "additive",
                               "blend mode", "blending mode", "blendmode")
# 5.4 真机误报教训：LogMaterial 的着色器统计行（Display 级、单条上百 KB）同时含
# "nanite" 与 "translucent"，只靠关键词会炸出一条 error。级别必须是 Warning/Error/Fatal。
_LOG_WARN_LEVEL_RE = re.compile(r":\s*(?:warning|error|fatal)\s*:", re.IGNORECASE)
# 冲突日志里的名字在方括号里且不带 /Game 路径（[M_X] / [SM_Y]），单独按名归因；
# 字符类只留标识符字符，天然排除 "[2026.09.29-08.00.00:000]" 这类时间戳前缀。
_LOG_BRACKET_NAME_RE = re.compile(r"\[\s*([A-Za-z0-9_.\-]{2,120}?)\s*\]")


def _rule_texture_streaming_overflow(ctx):
    """聚合池需求(上界) vs 工程 ini 里的 r.Streaming.PoolSize —— 给"总账"而非逐张。

    demand 来自本次采样纹理度量（覆盖面由 sample_size 决定，返回体 sampling 已标注）；
    pool 来自磁盘 ini（不需引擎）。判不出池就不下"超限"结论，只报需求（不猜默认值）。
    """
    th = ctx["profile"]["texture_streaming"]
    entries = _ensure_pool_entries(ctx)
    if not entries:
        return []
    agg = streaming.aggregate(entries)
    demand = float(agg["pool_demand_mb"])
    config = streaming.read_pool_config(ctx["project_dir"])
    verdict = streaming.compare_pool(demand, config["effective_mb"],
                                     tight_pct=float(th["tight_pct"]),
                                     min_demand_mb=float(th["min_demand_mb"]))
    if verdict["verdict"] == "ok":
        return []
    sev = "error" if verdict["verdict"] in ("overflow", "disabled") else "warn"
    # 与 opt_texture 工具同口径：池内 + 池外常驻都是要花的显存
    top = sorted(entries, key=lambda e: (-(float(e.get("pool_mb") or 0.0)
                                          + float(e.get("permanent_mb") or 0.0)),
                                         str(e.get("package"))))[:5]
    evidence = {
        "verdict": verdict["verdict"],
        "pool_demand_mb": verdict["demand_mb"],
        "pool_mb": verdict["pool_mb"],
        "overflow_mb": verdict["overflow_mb"],
        "ratio_pct": verdict["ratio_pct"],
        "permanent_mb": agg["permanent_mb"],
        "virtualized_mb": agg["virtualized_mb"],
        "potential_pool_release_mb": agg["potential_pool_release_mb"],
        "need_clamp_count": agg["need_clamp_count"],
        "need_vt_count": agg["need_vt_count"],
        "uncompressed_count": agg["uncompressed_count"],
        "non_streaming_count": agg["non_streaming_count"],
        "potential_permanent_release_mb": agg["potential_permanent_release_mb"],
        "size_unverified_count": agg["size_unverified_count"],
        "size_unverified_pool_mb": agg["size_unverified_pool_mb"],
        "size_unverified_permanent_mb": agg["size_unverified_permanent_mb"],
        "savings_withheld_count": agg["savings_withheld_count"],
        "measured_textures": agg["measured"],
        "pool_source": config["effective"],
        "pool_note": config["note"],
        "vt_enabled_project": streaming.vt_enabled(config),
        "top_contributors": [{"package": e["package"], "pool_mb": e["pool_mb"],
                              "permanent_mb": e.get("permanent_mb")}
                             for e in top if e.get("package")],
    }
    evidence = {k: v for k, v in evidence.items() if v not in (None, {}, [])}
    if verdict["verdict"] == "unknown":
        advice = ("工程 ini 未配 *PoolSize，%sMB 池需求无对照：先定池预算"
                  "（大工程常见根因是根本没配池，而不是池配小了），再按逐张规则降需求"
                  % demand)
    elif verdict["verdict"] == "disabled":
        advice = ("PoolSize<=0 等价不设上限（全部常驻），%sMB 直接压显存："
                  "先降需求（钳尺寸/虚拟化/改压缩）再开池" % demand)
    elif verdict["verdict"] == "overflow":
        advice = ("池需求(上界) %sMB > 配置池 %sMB：先用 opt_texture_clamp_max_size / "
                  "opt_texture_set_virtual / opt_texture_set_compression 降需求，"
                  "而不是一路加大 r.Streaming.PoolSize" % (demand, verdict["pool_mb"]))
    else:
        advice = ("池需求(上界) %sMB 已到配置池 %sMB 的告警线：按 oversized_mip/"
                  "uncompressed_texture 逐张降需求最划算" % (demand, verdict["pool_mb"]))
    if agg.get("size_unverified_count"):
        # 安全阀（#11）：总账里有多少是"边长没把握"的，摊开了说，不藏进合计
        advice += ("（其中 %d 张、约 %sMB 池需求建立在未通过校验的反推边长上，"
                   "另有 %d 项收益已被安全阀撤回：先载入复测再定预算）"
                   % (agg["size_unverified_count"], agg["size_unverified_pool_mb"],
                      agg["savings_withheld_count"]))
    return [{"rule_id": "texture_streaming_overflow", "severity": sev,
             "subject": "%s (scope)" % ctx["scope"],
             "evidence": evidence,
             "threshold": verdict["pool_mb"], "advice": advice}]


def _money_or_placeholder(value, format_text):
    """收益金额文案：None = 安全阀撤回报价（源边长反推不可信），不编数字也不谎报 0。"""
    if value is None:
        return u"金额暂不报价（源边长为反推估计，先载入复测）"
    return format_text % value


def _rule_oversized_mip(ctx):
    """未钳 MaxSize 的超大纹理：整条 mip 链进池，批量钳是成本最低的降需求手段。

    安全阀（#11）：源边长反推不可信时 clamp_savings_mb=None。此时仍出 finding
    （severity 封顶 warn、证据里不写金额）——"算不准收益"不等于"看不见问题"，
    直接 continue 会让 oversized_mip 整类隐身（#7 踩过同样的坑）。
    """
    th_px = ctx["profile"]["texture_px"]
    th = ctx["profile"]["mip_clamp"]
    out = []
    for e in _ensure_pool_entries(ctx):
        if "clamp_max_size" not in (e.get("actions") or []):
            continue
        saving = e.get("clamp_savings_mb")
        unverified = saving is None or "size_unverified" in (e.get("flags") or [])
        px = int(e.get("source_width") or e.get("effective_width") or 0)
        if unverified:
            sev, limit = "warn", float(th["min_savings_mb"])
        else:
            saving = float(saving or 0.0)
            if saving < float(th["min_savings_mb"]):
                continue
            if px >= int(th_px["error"]) and saving >= float(th["error_savings_mb"]):
                sev, limit = "error", int(th_px["error"])
            else:
                sev, limit = "warn", float(th["min_savings_mb"])
        evidence = {"source_width": px, "max_size": e.get("max_size"),
                    "clamp_to": e.get("clamp_to"), "savings_mb": saving,
                    "pool_mb": e.get("pool_mb"), "runtime_mb": e.get("runtime_mb"),
                    "virtual_texture": e.get("virtual_texture"),
                    "compression": e.get("compression"),
                    "effective_width": e.get("effective_width"),
                    "size_basis": e.get("size_basis")}
        if unverified:
            evidence["size_unverified"] = True
        evidence = {k: v for k, v in evidence.items() if v is not None}
        advice = ("整条 mip 链进池：opt_texture_clamp_max_size(max_size=%s) "
                  "批量钳（先 dry-run 看每张 before→after）" % e.get("clamp_to"))
        if unverified:
            advice += ("；源边长 %s 是反推估计值，省额不报价：先用 "
                       "get_asset_metrics/scan_texture_streaming 载入复测再定钳到多少" % px)
        out.append({"rule_id": "oversized_mip", "severity": sev,
                    "subject": e["package"], "evidence": evidence, "threshold": limit,
                    "advice": advice})
    return out


def _rule_uncompressed_texture(ctx):
    """非工程运行时压缩格式，三本账分开算（同一个 MB 不重复报价）。

    - uncompressed（真未压缩/浮点系）：按 4B/px 计，比 BC 贵 4 倍，池与包体双输；
    - high_bpp（TC_HDR 高位深）：引擎不做块压缩，5.4 实测按 8B/px 常驻；改成压缩格式
      能省约 7/8，但会丢 HDR 动态范围，属美术决策 -> 只报账，不进默认改写动作；
    - no_stream（TC_EDITOR_ICON / TC_MAX）：无 mip + 禁用流送，整张恒常驻池外，
      收益口径是"改回 TC_Default 变可流送、释放常驻"，不是省池。
    判定用 streaming.format_class（与 opt_texture_set_compression 同一函数），
    unknown/本地化文本一律不报，宁可漏报也不误判美术格式。
    格式是直读属性、不依赖边长，所以安全阀只撤金额（savings 为 None 时建议与
    动作照给，证据里不编数字）。
    """
    th = ctx["profile"]["uncompressed_texture"]
    out = []
    for e in _ensure_pool_entries(ctx):
        fmt = e.get("format_class") or streaming.format_class(e.get("compression"))
        if fmt not in ("uncompressed", "no_stream", "high_bpp"):
            continue
        mb = float(e.get("runtime_mb") or 0.0)
        if mb < float(th["min_mb"]):
            continue
        sev = "error" if mb >= float(th["error_mb"]) else "warn"
        if fmt == "no_stream":
            saving = e.get("streaming_release_mb")
            advice = ("%s 无 mip/禁用流送：整张恒常驻池外 %sMB（不进池但死占显存，"
                      "VT/钳尺寸救不了）；改回 TC_Default 可变可流送，"
                      "opt_texture_set_compression 默认只动这类（需重压缩）"
                      % (e.get("compression"), mb))
        elif fmt == "high_bpp":
            saving = e.get("compression_savings_mb")
            advice = ("%s 高位深不降块压缩（实测 %gB/px）：整张 %sMB 死占池/显存；改回 "
                      "TC_Default %s，但会丢 HDR 动态范围，属美术决策 —— "
                      "opt_texture_set_compression 默认不动这类，要动需显式 "
                      "only_uncompressed=False，且改完要重压缩才反映到盘上"
                      % (e.get("compression"),
                         float(e.get("runtime_bytes_per_texel") or 8), mb,
                         _money_or_placeholder(saving, "约省 %sMB")))
        else:
            saving = e.get("compression_savings_mb")
            advice = ("未压缩系(%s)按 %gB/px 常驻，改 TC_Default %s；"
                      "opt_texture_set_compression 默认只动这类（需重压缩）"
                      % (e.get("compression"),
                         float(e.get("runtime_bytes_per_texel") or 4),
                         _money_or_placeholder(saving, "可省约 %sMB")))
        evidence = {"compression": e.get("compression"), "format_class": fmt,
                    "runtime_mb": mb, "pool_mb": e.get("pool_mb"),
                    "permanent_mb": e.get("permanent_mb"), "savings_mb": saving,
                    "runtime_bytes_per_texel": e.get("runtime_bytes_per_texel"),
                    "runtime_basis": e.get("runtime_basis"),
                    "source_width": e.get("source_width"),
                    "size_basis": e.get("size_basis"),
                    "virtual_texture": e.get("virtual_texture")}
        if saving is None or "size_unverified" in (e.get("flags") or []):
            evidence["size_unverified"] = True
        evidence = {k: v for k, v in evidence.items() if v is not None}
        out.append({"rule_id": "uncompressed_texture", "severity": sev,
                    "subject": e["package"], "evidence": evidence,
                    "threshold": float(th["error_mb"] if sev == "error" else th["min_mb"]),
                    "advice": advice})
    return out


def _rule_nanite_translucent_conflict(ctx):
    """编辑器日志里"Nanite 网格 + 半透明材质"的静默降级警告，按指纹归因到资产。

    离线通道（直读 Saved/Logs）。这条专门抓"日志刷屏但没人知道是谁"的场景：
    5.3 真实工程实测 1.4 万+ 条 Warning 几乎全同源。
    """
    th = ctx["profile"]["nanite_translucent"]
    log_path = logscan._find_log(ctx["project_dir"])
    lines = logscan._tail_lines(log_path, int(th["log_tail"]))
    hits = 0
    assets = {}
    names = {}
    samples = []
    for raw in lines:
        low = raw.lower()
        if "nanite" not in low:
            continue
        if not any(mark in low for mark in _NANITE_TRANSLUCENT_PHRASES):
            continue
        if not any(mark in low for mark in _NANITE_TRANSLUCENT_MARKERS):
            continue
        if not _LOG_WARN_LEVEL_RE.search(raw):
            continue
        hits += 1
        found = []
        for match in attributelog.GAME_RE.findall(raw):
            pkg = match.split(".", 1)[0]
            if pkg not in found:
                found.append(pkg)
        for pkg in found:
            assets[pkg] = assets.get(pkg, 0) + 1
        for name in _LOG_BRACKET_NAME_RE.findall(raw):
            if not any(ch.isalpha() for ch in name):
                continue          # 帧计数 "[  12]" 之类纯数字不是资产名
            if name.lower().startswith("blend"):
                continue          # 5.4 同行尾还带 [BLEND_Translucent]，是枚举不是资产名
            names[name] = names.get(name, 0) + 1
        if len(samples) < 3:
            samples.append(raw.strip()[:200])
    if hits < int(th["min_count"]):
        return []
    top = sorted(assets.items(), key=lambda kv: (-kv[1], kv[0]))[:10]
    top_names = sorted(names.items(), key=lambda kv: (-kv[1], kv[0]))[:10]
    if top:
        attribution = "package_paths"
    elif top_names:
        attribution = "asset_names_only"
    else:
        attribution = "none"
    evidence = {"warning_lines": hits, "distinct_assets": len(assets),
                "assets": [{"package": k, "count": v} for k, v in top],
                "attribution": attribution,
                "distinct_names": len(names),
                "names": [{"name": k, "count": v} for k, v in top_names],
                "log": os.path.basename(log_path), "samples": samples}
    evidence = {k: v for k, v in evidence.items() if v not in (None, {}, [])}
    if attribution == "package_paths":
        subject = "%s (%d assets)" % (evidence.get("log", "editor log"), len(assets))
    elif attribution == "asset_names_only":
        subject = ("%s (%d 条冲突；UE 该行只带资产名不带 /Game 路径，"
                   "按名归因见 evidence.names)" % (evidence.get("log", "editor log"), hits))
    else:
        subject = "%s (%d 条冲突；日志未给出可归因的资产名)" % (
            evidence.get("log", "editor log"), hits)
    return [{"rule_id": "nanite_translucent_conflict", "severity": "error",
             "subject": subject, "evidence": evidence, "threshold": int(th["min_count"]),
             "advice": "Nanite 不支持 Translucent 系混合模式：网格会静默降级并刷日志。"
                       "用 scan_nanite_candidates 定位，再 fix_nanite_translucent "
                       "先 dry-run、双钥关闭（关 Nanite 是降级不是损坏）"}]


def _rule_drawcall_batching(ctx):
    """重复同网格的散装 StaticMeshActor = HISM/ISM 合批机会（drawcall 白给）。

    数据来自 list_level_actors(compose=True) 的 composition，口径与该工具一致；
    composition 覆盖整关卡（不受 actors limit 截断），故这里判的是完整场景。
    """
    th = ctx["profile"]["batching"]
    comp = _ensure_composition(ctx)
    if not isinstance(comp, dict):
        return []
    opps = comp.get("instancing_opportunities") or []
    if not opps:
        return []
    min_group = int(th["min_group_actors"])
    opps = [o for o in opps if int(o.get("actors") or 0) >= min_group]
    if not opps:
        return []
    collapsible = sum(int(o["actors"]) - 1 for o in opps)
    if collapsible < int(th["min_collapsible"]):
        return []
    share = float(comp.get("instanced_share_pct") or 0.0)
    sev = "error" if share <= 0.0 else "warn"
    evidence = {"groups": len(opps), "collapsible_actors": collapsible,
                "instanced_share_pct": share,
                "plain_staticmesh_actors": comp.get("plain_staticmesh_actors"),
                "world_total_actors": comp.get("world_total_actors"),
                "top_groups": opps[:10], "min_group_actors": min_group}
    return [{"rule_id": "drawcall_batching", "severity": sev,
             "subject": "level (%d groups)" % len(opps), "evidence": evidence,
             "threshold": int(th["min_collapsible"]),
             "advice": "同网格重复放置未实例化：analyze_instancing 出合并方案（路线 B "
                       "只出计划不动场景）；真要折叠成 HISM 须先提交 VCS/另存关卡副本"}]


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
    {"id": "texture_streaming_overflow", "cfg": "texture_streaming", "channel": "bridge",
     "doc": "聚合流送池需求 vs 工程 ini 的 PoolSize（总账，不逐张）",
     "run": _rule_texture_streaming_overflow},
    {"id": "oversized_mip", "cfg": "mip_clamp", "channel": "bridge",
     "doc": "未钳 MaxSize 的超大纹理：整条 mip 链进池，批量钳收益/成本比最高"
          "（源边长反推不可信时仍报：severity 封顶 warn、evidence.size_unverified、不编金额）",
     "run": _rule_oversized_mip},
    {"id": "uncompressed_texture", "cfg": "uncompressed_texture", "channel": "bridge",
     "doc": "非工程运行时压缩格式：未压缩系 4B/px、TC_HDR 高位深 8B/px 吃池；EditorIcon 系无 mip/禁流送恒常驻"
          "（格式是直读属性，安全阀只撤金额不撤判定）",
     "run": _rule_uncompressed_texture},
    {"id": "nanite_translucent_conflict", "cfg": "nanite_translucent", "channel": "offline",
     "doc": "编辑器日志里 Nanite+半透明材质的静默降级警告（按指纹归因资产）",
     "run": _rule_nanite_translucent_conflict},
    {"id": "drawcall_batching", "cfg": "batching", "channel": "bridge",
     "doc": "重复同网格散装 StaticMeshActor 的 HISM/ISM 合批机会",
     "run": _rule_drawcall_batching},
)


# ---- ROI 注解（发现 -> 工单：给每条 finding 粗量化收益 + 改动成本档）----
# 原则：宁可粗档，不造伪精确。体量类给 MB，渲染/正确性类给可数代理指标或标注 correctness。
_EFFORT_BY_RULE = {
    "asset_size_top": "mid", "texture_size": "mid", "mesh_tri": "high",
    "wps_external_actors": "mid", "scene_light_dup": "low",
    "cook_drop": "high", "cook_empty_maps": "high",
    # v1.5 优化族：纯属性改动(钳/VT/压缩)=low，改网格=mid，改关卡 Actor=high
    "oversized_mip": "low", "uncompressed_texture": "low",
    "texture_streaming_overflow": "mid", "nanite_translucent_conflict": "mid",
    "drawcall_batching": "high",
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
    elif rid == "oversized_mip":
        unit, benefit = "memory_or_disk_mb", ev.get("savings_mb")
        rationale = "opt_texture_clamp_max_size 批量钳 MaxSize：纯属性改动，收益=省下的池/显存"
    elif rid == "uncompressed_texture":
        fmt_name = ev.get("format_class")
        # 与 oversized_mip 的钳尺寸收益有重叠，故按本分列，不并入 MB 总计
        if fmt_name == "no_stream":
            unit, benefit = "permanent_release_mb", ev.get("savings_mb")
            rationale = ("无 mip/禁流送系：改 TC_Default 释放的是常驻显存而非池；"
                         "与钳尺寸收益重叠，不叠加计入总计")
        elif fmt_name == "high_bpp":
            unit, benefit = "pool_high_bpp_mb", ev.get("savings_mb")
            rationale = ("高位深(TC_HDR)按 8B/px 常驻：改块压缩约省 7/8，但会丢 HDR 动态范围，"
                         "属美术决策(默认工具不代改)；与钳尺寸收益重叠，不叠加计入总计")
        else:
            unit, benefit = "pool_uncompressed_mb", ev.get("savings_mb")
            rationale = "改压缩格式(需重压缩)可省约 3/4；与钳尺寸收益重叠，不叠加计入总计"
    elif rid == "texture_streaming_overflow":
        unit, benefit = "pool_overflow_mb", ev.get("overflow_mb")
        rationale = "总账：降需求(钳/VT/压缩)才是正解，单加大 PoolSize 只是掩盖"
    elif rid == "nanite_translucent_conflict":
        unit, benefit = "warning_lines", ev.get("warning_lines")
        rationale = "关 Nanite 消静默降级与日志刷屏；收益是正确性/可观测性，不是体量"
    elif rid == "drawcall_batching":
        unit, benefit = "collapsible_actors", ev.get("collapsible_actors")
        rationale = "HISM 合批要动关卡 Actor，风险最高（路线 B 先只出方案），不计入体量收益"
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
