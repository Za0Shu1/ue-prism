"""scan_folder_assets 离线磁盘扫描测试（纯标准库，无引擎、无 mcp）。"""
from __future__ import annotations

import os

from prism import folderscan


def _make_project(tmp_path):
    c = tmp_path / "Content"
    (c / "Props").mkdir(parents=True)
    files = {
        "Content/Foo.uasset": 10,
        "Content/Bar.umap": 5,
        "Content/Props/Baz.uasset": 20,
        "Content/Props/Baz.uasset.bak": 999,  # 应被跳过
        "Content/readme.txt": 12345,          # 非资产，跳过
    }
    for rel, size in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(str(p), "wb") as f:
            f.write(b"\0" * size)
    return str(tmp_path)


def test_scan_sort_and_totals(tmp_path):
    r = folderscan.scan_folder_assets(_make_project(tmp_path))
    assert r["source"] == "disk"
    assert r["scanned_files"] == 3          # .bak 与 .txt 被排除
    assert r["total_bytes"] == 35           # 10 + 5 + 20
    assert r["by_type"]["uasset"]["count"] == 2
    assert r["by_type"]["umap"]["count"] == 1
    assert r["assets"][0]["asset_path"] == "/Game/Props/Baz"   # 最大在前
    assert r["assets"][0]["size_bytes"] == 20


def test_folder_subtree_and_cap(tmp_path):
    r = folderscan.scan_folder_assets(_make_project(tmp_path), folder="/Game/Props", limit=1)
    assert r["scanned_files"] == 1
    assert r["truncated"] is False
    assert r["assets"][0]["asset_path"] == "/Game/Props/Baz"