"""v0.2 PR-3 日志→资产归因（docs/DESIGN_v0.2.md §5）：纯标准库离线解析；富化单次经总线。

流水线：
  1) 错误行识别（LogXxx: Error / Fatal / UAT "ERROR:" 三形态）
  2) 指纹分组：路径与数字归一化，同类错误聚成 pattern
  3) 行内提取 /Game 包路径（含 Content\\...uasset 相对形态）
  4) 磁盘解析（存在性/大小；离线可用）
  5) bridge 在线（心跳新鲜，PR-0.5 基建）时批量富化：describe_many + referencers_many
     各一次总线往返（绝不逐资产 N 次 —— 设计稿洞②）
列表纪律：pattern ≤ top（默认 30），组内资产 ≤ 10；total/truncated/cap 全程携带。
"""
from __future__ import annotations

import os
import re

from . import bus, envelope

GAME_RE = re.compile(r"/Game/[A-Za-z0-9_]+(?:/[A-Za-z0-9_]+)*(?:\.[A-Za-z0-9_]+)?")
CONTENT_REL_RE = re.compile(r"Content[/\\]+([^\s\"']+?\.(?:uasset|umap))")
_TS_RE = re.compile(r"^\[[^\]]*\](\[\s*\d*\])?")
_LOG_ERR_RE = re.compile(r"^([A-Za-z0-9_]+):\s*(Error|Fatal error|Fatal|ERROR)\s*:?\s*(.*)$")
_UAT_ERR_RE = re.compile(r"^ERROR:\s*(.*)$")
_FATAL_ERR_RE = re.compile(r"^Fatal\s*[Ee]rror\s*[:!]\s*(.*)$")
_NUM_RE = re.compile(r"\d+")

ENRICH_CAP = 60        # 单次总线往返最多带走的资产数
PER_GROUP_CAP = 10     # 每 pattern 展示资产上限


def _line_category_message(raw):
    """返回 (category, message) 或 None。时间戳剥离后按三形态匹配。"""
    line = _TS_RE.sub("", raw.strip())
    m = _UAT_ERR_RE.match(line)
    if m:
        return ("UAT", m.group(1))
    m = _LOG_ERR_RE.match(line)
    if m:
        return (m.group(1), m.group(3))
    m = _FATAL_ERR_RE.match(line)
    if m:
        return ("FatalError", m.group(1))
    return None


def _fingerprint(category, message):
    s = GAME_RE.sub("<ASSET>", message)
    s = CONTENT_REL_RE.sub("<ASSET>", s)
    s = _NUM_RE.sub("#", s)
    return (category + ": " + s.strip())[:200]


def _asset_candidates(line):
    out = []
    for m in GAME_RE.finditer(line):
        pkg = m.group(0).split(".", 1)[0] if "." in m.group(0).split("/")[-1] else m.group(0)
        if pkg not in out:
            out.append(pkg)
    for m in CONTENT_REL_RE.finditer(line):
        rel = m.group(1).replace("\\", "/")
        rel = rel[: rel.rfind(".")]  # 去扩展名
        pkg = "/Game/" + rel if not rel.startswith("Game/") else rel
        if pkg not in out:
            out.append(pkg)
    return out


def _disk_info(content_root, pkg):
    rel = pkg[len("/Game/"):].replace("/", os.sep) if pkg.startswith("/Game/") else None
    if rel is None:
        return {"path": pkg, "exists": False, "size_bytes": None}
    for ext in (".uasset", ".umap"):
        p = os.path.join(content_root, rel + ext)
        if os.path.isfile(p):
            try:
                return {"path": pkg, "exists": True, "size_bytes": os.path.getsize(p)}
            except OSError:
                pass
    return {"path": pkg, "exists": False, "size_bytes": None}


def parse_log(log_path, project_dir=None, top=30):
    """解析 cook/编辑器日志 -> 归因报告骨架（未富化）。编码容错 utf-8-sig + replace。"""
    top = max(1, int(top))
    content_root = os.path.join(project_dir, "Content") if project_dir else None
    groups = {}
    order = []
    scanned = 0
    error_lines = 0
    with open(log_path, "r", encoding="utf-8-sig", errors="replace") as f:
        for i, raw in enumerate(f):
            scanned += 1
            cm = _line_category_message(raw)
            if cm is None:
                continue
            category, message = cm
            error_lines += 1
            fp = _fingerprint(category, message)
            g = groups.get(fp)
            if g is None:
                g = groups[fp] = {"pattern": fp, "count": 0, "first_line_no": i + 1,
                                  "sample_line": raw.strip()[:500], "assets": {}}
                order.append(fp)
            g["count"] += 1
            for pkg in _asset_candidates(raw):
                if pkg not in g["assets"]:
                    info = _disk_info(content_root, pkg) if content_root else {"path": pkg}
                    g["assets"][pkg] = info
    patterns = []
    for fp in sorted(order, key=lambda k: -groups[k]["count"]):
        g = groups[fp]
        assets = sorted(g["assets"].values(), key=lambda a: (-int(a.get("size_bytes") or 0), a["path"]))
        patterns.append({
            "pattern": g["pattern"],
            "count": g["count"],
            "first_line_no": g["first_line_no"],
            "sample_line": g["sample_line"],
            "assets": assets[:PER_GROUP_CAP],
            "assets_total": len(assets),
            "assets_truncated": len(assets) > PER_GROUP_CAP,
        })
    total = len(patterns)
    return {
        "log_path": os.path.abspath(log_path),
        "scanned_lines": scanned,
        "error_lines": error_lines,
        "patterns": patterns[:top],
        "total": total,
        "truncated": total > top,
        "cap": top,
        "enriched": False,
        "enrich_note": "offline",
    }


def all_assets(report):
    out = []
    for g in report["patterns"]:
        for a in g["assets"]:
            if a["path"] not in out:
                out.append(a["path"])
    return out


def enrich_via_bus(bus_dir, paths):
    """心跳新鲜才发起（不付 30s 探测税）；返回 (map|None, note)。"""
    if not paths:
        return None, "no_assets"
    age = bus.heartbeat_age(bus_dir)
    if age is None or age > bus.HB_STALE_SECONDS:
        return None, "bridge offline or heartbeat missing/stale"
    client = bus.BusClient(bus_dir, timeout=10)
    want = list(paths[:ENRICH_CAP])
    d = client.call("describe_many", {"paths": want})
    r = client.call("referencers_many", {"paths": want, "cap": 10})
    if not d.get("ok"):
        return None, "describe_many failed: %s" % (d.get("error") or {}).get("code", "?")
    emap = {}
    for item in d["result"]["items"]:
        if item.get("ok") is False:  # 领域函数自返信封（如 UE_API_MISMATCH）
            continue
        key = item.get("package_name") or item.get("query")
        if key:
            emap.setdefault(key, {}).update(
                {"class": item.get("class"), "found": item.get("found"),
                 "size_bytes": item.get("size_bytes")})
    if r.get("ok"):
        for item in r["result"]["items"]:
            key = item.get("package_name") or item.get("query")
            if key:
                emap.setdefault(key, {})["used_by_count"] = item.get("used_by_count")
    note = "enriched %d/%d assets" % (len(emap), len(want))
    if len(paths) > ENRICH_CAP:
        note += " (capped at %d)" % ENRICH_CAP
    return emap, note


def apply_enrichment(report, emap, note):
    if not emap:
        report["enrich_note"] = note
        return
    for g in report["patterns"]:
        for a in g["assets"]:
            e = emap.get(a["path"]) or {}
            for k in ("class", "used_by_count", "found"):
                if k in e and e[k] is not None:
                    a[k] = e[k]
        g["assets"].sort(key=lambda a: (-(int(a.get("used_by_count") or 0)),
                                        -int(a.get("size_bytes") or 0), a["path"]))
    report["enriched"] = True
    report["enrich_note"] = note
