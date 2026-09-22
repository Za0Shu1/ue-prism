"""v0.3 性能规则引擎（server 侧；docs/DESIGN_v0.3.md）。

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
           "mesh_tri": {"warn": 200000, "error": 1000000},
           "wps_external_actors": {"warn": 300, "error": 1000},
           "scene_light_dup": {"max_dup": 1}},
    "console": {"asset_size_mb": {"warn": 10.0, "error": 50.0},
                "texture_px": {"warn": 2048, "error": 4096},
                "mesh_tri": {"warn": 150000, "error": 500000},
                "wps_external_actors": {"warn": 300, "error": 1000},
                "scene_light_dup": {"max_dup": 1}},
    "mobile": {"asset_size_mb": {"warn": 5.0, "error": 20.0},
               "texture_px": {"warn": 1024, "error": 2048},
               "mesh_tri": {"warn": 50000, "error": 150000},
               "wps_external_actors": {"warn": 200, "error": 600},
               "scene_light_dup": {"max_dup": 1}},
}
DEFAULT_CAP = 50
DEFAULT_RECENT_TASKS = 3
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
        ctx["scan"] = folderscan.scan_folder_assets(ctx["project_dir"], folder=ctx["scope"], limit=2000)
    return ctx["scan"]


def _ensure_classes(ctx):
    if "classes" not in ctx:
        classes = {}
        scan = _ensure_scan(ctx)
        cands = [a["asset_path"] for a in scan["assets"][:80]]
        if cands:
            d = bus.BusClient(ctx["bus_dir"], timeout=60).call("describe_many", {"paths": cands, "limit": 80})
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
                seen.add(key)
                reported.add(key)
                hits += 1
                out.append({"rule_id": "cook_drop", "severity": "error",
                            "subject": found[0] if found else rec["task_id"],
                            "evidence": {"task_id": rec["task_id"], "line_no": i + 1,
                                         "task_created_ts": rec.get("created_ts"),
                                         "excerpt": raw.strip()[:200]},
                            "threshold": None,
                            "advice": "cook succeeded but content was dropped; restore/fix the missing dependency"})
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
        px = int(m.get("width") or 0)
        if px <= 0:
            continue
        if px >= int(th["error"]):
            sev, limit = "error", int(th["error"])
        elif px >= int(th["warn"]):
            sev, limit = "warn", int(th["warn"])
        else:
            continue
        out.append({"rule_id": "texture_size", "severity": sev, "subject": pkg,
                    "evidence": {"width": px, "height": m.get("height"), "memory_bytes": m.get("memory_bytes")},
                    "threshold": limit, "advice": "downscale, virtualize, or use texture LOD bias"})
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
        advice = "reduce complexity / add LODs"
        if (m.get("lod_count") or 0) <= 1:
            advice = "single-LOD high-poly mesh: author LODs first"
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
    return [{"rule_id": "scene_light_dup", "severity": "warn", "subject": ctx.get("scope", "/Game"),
             "evidence": {"counts": dups, "actors_total": len(actors)}, "threshold": int(th["max_dup"]),
             "advice": "duplicate directional/skylights cost double lighting passes; keep one of each"}]


def _map_cooked_on_disk(project_dir, map_key):
    """增量 cook 全保留(no-op)时日志不会有任何该 map 的逐包行——用磁盘 cook 产物做第二证据源。

    Saved/Cooked/<平台>/<工程>/Content/<地图>.umap 存在 = 曾被成功 cook（本会话只是没重煮它）。
    """
    stem = map_key.rpartition("/")[2]
    pat = os.path.join(str(project_dir), "Saved", "Cooked", "*", "*", "Content", stem + ".umap")
    return bool(glob.glob(pat))


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


RULES = (
    {"id": "asset_size_top", "cfg": "asset_size_mb", "channel": "offline", "doc": "单资产磁盘大小超阈值", "run": _rule_asset_size_top},
    {"id": "cook_drop", "cfg": None, "channel": "offline", "doc": "cook succeeded 但日志含静默丢弃依赖告警（校准发现③）", "run": _rule_cook_drop},
    {"id": "cook_empty_maps", "cfg": None, "channel": "offline", "doc": "请求的 map 从未被 cook / 0 包空 cook（校准发现②）", "run": _rule_cook_empty_maps},
    {"id": "wps_external_actors", "cfg": "wps_external_actors", "channel": "offline", "doc": "WP 外部 actor 包数量超阈值（真机案例 148 包）", "run": _rule_wps_external_actors},
    {"id": "texture_size", "cfg": "texture_px", "channel": "bridge", "doc": "纹理尺寸超阈值（top-80 大资产抽样度量）", "run": _rule_texture_size},
    {"id": "mesh_tri", "cfg": "mesh_tri", "channel": "bridge", "doc": "网格 LOD0 三角数超阈值/单 LOD 高模", "run": _rule_mesh_tri},
    {"id": "scene_light_dup", "cfg": "scene_light_dup", "channel": "bridge", "doc": "DirectionalLight/SkyLight 重复放置", "run": _rule_scene_light_dup},
)


def run_report(project_dir, bus_dir, scope="/Game", target=None, cap=DEFAULT_CAP,
               recent_tasks=DEFAULT_RECENT_TASKS):
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
    ctx = {"project_dir": project_dir, "bus_dir": bus_dir, "scope": scope, "profile": profile,
           "recent_tasks": recent_tasks}
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
    window_ids = [r["task_id"] for r in _cook_records(ctx)]
    rank = {"error": 0, "warn": 1}
    findings.sort(key=lambda x: (rank.get(x["severity"], 9), x["rule_id"], x["subject"]))  # 稳定序：diff 友好
    total = len(findings)
    summary = {"error": sum(1 for x in findings if x["severity"] == "error"),
               "warn": sum(1 for x in findings if x["severity"] == "warn"),
               "skipped_rules": skipped}
    return {
        "profile": used_target,
        "scope": scope,
        "bridge": "online" if bridge_online else "offline",
        "engine": None,
        "note": "bridge rules measure top-80 largest assets (sampled, not exhaustive)" if bridge_online
        else "offline half-report (bridge rules skipped)",
        "summary": summary,
        "cook_archive": {"recent_tasks": recent_tasks,
                         "window_task_ids": window_ids,
                         "archive_total": ctx.get("cook_recs_total", len(window_ids))},
        "findings": findings[:cap],
        "total": total,
        "truncated": total > cap,
        "cap": cap,
    }


def list_rules(target=None, project_dir=""):
    profile, used_target = load_profile(project_dir, target)
    return {"target": used_target,
            "rules": [{"id": r["id"], "channel": r["channel"], "doc": r["doc"],
                       "thresholds": profile.get(r["cfg"]) if r["cfg"] else None}
                      for r in RULES],
            "total": len(RULES), "truncated": False, "cap": len(RULES)}
