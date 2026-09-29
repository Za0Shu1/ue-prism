"""scan_folder_assets 的离线磁盘扫描：纯标准库，不依赖 UE/bridge。

把 /Game/... 虚拟路径映射到 <project>/Content/... 实际目录，按磁盘大小排序、
按类型（uasset/umap）汇总占用，并按目录做聚合预算（哪个子目录最重、占比多少），
给出 Top 开销文件。对应 REQUIREMENTS §2 S2 底座 + §0.4 聚合预算。
注意：读的是磁盘上已保存的 .uasset/.umap（离线快照），非编辑器内存态。
"""
from __future__ import annotations

import os

_EXTS = {"uasset", "umap"}
_MB = 1048576.0


def _virtual_path(content_root, file_path):
    rel = os.path.relpath(file_path, content_root).replace("\\", "/")
    stem, dot, ext = rel.rpartition(".")
    if dot and ext.lower() in _EXTS:
        rel = stem
    return "/Game/" + rel


def _disk_root(project_dir, folder):
    content_root = os.path.join(project_dir, "Content")
    if not folder or folder in ("/Game", "/Game/"):
        return content_root
    sub = folder[len("/Game"):].lstrip("/") if folder.startswith("/Game") else folder
    parts = [p for p in sub.split("/") if p]
    return os.path.join(content_root, *parts) if parts else content_root


def _norm_virtual(folder):
    """把扫描的 /Game 虚拟目录规范化为基路径（用于预算桶标签）。"""
    f = (folder or "").strip()
    if not f or f in ("/Game", "/Game/"):
        return "/Game"
    if not f.startswith("/Game"):
        f = "/Game/" + f.lstrip("/")
    return f.rstrip("/")


def _bucket_label(base_virtual, rel_to_root, depth):
    """按相对扫描根的路径取前 depth 级目录作为预算桶；根下散文件归到 base_virtual。

    depth<=0 表示用到叶子目录整条相对链（最细粒度）。"""
    parts = [p for p in rel_to_root.replace("\\", "/").split("/") if p]
    chain = parts[:-1]  # 去掉文件名
    take = chain if depth <= 0 else chain[:depth]
    if take:
        return base_virtual + "/" + "/".join(take)
    return base_virtual


def scan_folder_assets(project_dir, folder="/Game", sort="size", limit=500,
                       budget_depth=1, budget_cap=30):
    content_root = os.path.join(project_dir, "Content")
    if not os.path.isdir(content_root):
        raise FileNotFoundError("no Content dir: " + content_root)
    root = _disk_root(project_dir, folder)
    if not os.path.isdir(root):
        raise FileNotFoundError("no folder: " + root)

    limit = int(limit)
    budget_depth = int(budget_depth)
    budget_cap = int(budget_cap)
    base_virtual = _norm_virtual(folder)

    assets = []
    by_type = {}
    by_dir = {}
    total_bytes = 0
    for dirpath, _dirs, files in os.walk(root):
        for fn in files:
            ext = fn.rpartition(".")[2].lower()
            if ext not in _EXTS or fn.lower().endswith(".bak"):
                continue
            fp = os.path.join(dirpath, fn)
            try:
                size = os.path.getsize(fp)
            except OSError:
                continue
            total_bytes += size
            assets.append({
                "asset_path": _virtual_path(content_root, fp),
                "type": ext,
                "size_bytes": size,
                "size_mb": round(size / _MB, 3),
            })
            t = by_type.setdefault(ext, {"count": 0, "bytes": 0})
            t["count"] += 1
            t["bytes"] += size
            rel = os.path.relpath(fp, root)
            label = _bucket_label(base_virtual, rel, budget_depth)
            b = by_dir.setdefault(label, {"count": 0, "bytes": 0})
            b["count"] += 1
            b["bytes"] += size
    for t in by_type.values():
        t["mb"] = round(t["bytes"] / _MB, 3)

    dir_items = []
    for label, b in by_dir.items():
        dir_items.append({
            "path": label,
            "count": b["count"],
            "bytes": b["bytes"],
            "mb": round(b["bytes"] / _MB, 3),
            "pct_of_total": round(b["bytes"] * 100.0 / total_bytes, 2) if total_bytes else 0.0,
        })
    dir_items.sort(key=lambda d: d["bytes"], reverse=True)

    if sort == "size":
        assets.sort(key=lambda a: a["size_bytes"], reverse=True)
    else:  # path
        assets.sort(key=lambda a: a["asset_path"])

    return {
        "project_dir": project_dir,
        "root": root,
        "folder": folder,
        "source": "disk",
        "scanned_files": len(assets),
        "total_bytes": total_bytes,
        "total_mb": round(total_bytes / _MB, 3),
        "by_type": by_type,
        "by_dir": {
            "depth": budget_depth,
            "items": dir_items[:budget_cap] if budget_cap > 0 else dir_items,
            "total": len(dir_items),
            "truncated": budget_cap > 0 and len(dir_items) > budget_cap,
            "cap": budget_cap,
        },
        "sort": sort,
        "cap": limit,
        "truncated": len(assets) > limit,
        "assets": assets[:limit],
    }
