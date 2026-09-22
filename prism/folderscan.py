"""scan_folder_assets 的离线磁盘扫描：纯标准库，不依赖 UE/bridge。

把 /Game/... 虚拟路径映射到 <project>/Content/... 实际目录，按磁盘大小排序、
按类型（uasset/umap）汇总占用，给出 Top 开销文件。对应 docs/REQUIREMENTS.md §2 S2 底座。
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


def scan_folder_assets(project_dir, folder="/Game", sort="size", limit=500):
    content_root = os.path.join(project_dir, "Content")
    if not os.path.isdir(content_root):
        raise FileNotFoundError("no Content dir: " + content_root)
    root = _disk_root(project_dir, folder)
    if not os.path.isdir(root):
        raise FileNotFoundError("no folder: " + root)

    limit = int(limit)
    assets = []
    by_type = {}
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
    for t in by_type.values():
        t["mb"] = round(t["bytes"] / _MB, 3)

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
        "sort": sort,
        "cap": limit,
        "truncated": len(assets) > limit,
        "assets": assets[:limit],
    }