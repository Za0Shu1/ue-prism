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


def _labels(r):
    return {d["path"]: d for d in r["by_dir"]["items"]}


def test_by_dir_budget(tmp_path):
    r = folderscan.scan_folder_assets(_make_project(tmp_path))
    bd = r["by_dir"]
    assert bd["depth"] == 1
    assert bd["total"] == 2 and bd["truncated"] is False
    d = _labels(r)
    # 根下散文件 Foo(10)+Bar(5) 归 /Game；Props/Baz(20) 归 /Game/Props
    assert d["/Game"]["count"] == 2 and d["/Game"]["bytes"] == 15
    assert d["/Game/Props"]["count"] == 1 and d["/Game/Props"]["bytes"] == 20
    # 按 bytes 降序
    assert bd["items"][0]["path"] == "/Game/Props"
    # pct 合计 ~100
    assert abs(sum(x["pct_of_total"] for x in bd["items"]) - 100.0) < 0.05
    # 预算桶体量合计 == 总字节
    assert sum(x["bytes"] for x in bd["items"]) == r["total_bytes"]


def test_by_dir_subtree_base(tmp_path):
    r = folderscan.scan_folder_assets(_make_project(tmp_path), folder="/Game/Props")
    d = _labels(r)
    # 子树扫描：桶基路径应为该子目录本身
    assert list(d.keys()) == ["/Game/Props"]
    assert d["/Game/Props"]["bytes"] == 20


def test_by_dir_depth_leaf(tmp_path):
    c = tmp_path / "Content" / "A" / "B" / "C"
    c.mkdir(parents=True)
    with open(str(c / "deep.uasset"), "wb") as f:
        f.write(b"\0" * 7)
    r1 = folderscan.scan_folder_assets(str(tmp_path), budget_depth=1)
    d1 = _labels(r1)
    assert "/Game/A" in d1 and d1["/Game/A"]["bytes"] == 7
    r0 = folderscan.scan_folder_assets(str(tmp_path), budget_depth=0)
    d0 = _labels(r0)
    # depth<=0：聚合到叶子目录全链
    assert "/Game/A/B/C" in d0 and d0["/Game/A/B/C"]["bytes"] == 7


def test_by_dir_cap_truncated(tmp_path):
    c = tmp_path / "Content"
    for name, sz in (("A", 1), ("B", 2), ("C", 3), ("D", 4)):
        (c / name).mkdir(parents=True)
        with open(str(c / name / "x.uasset"), "wb") as f:
            f.write(b"\0" * sz)
    r = folderscan.scan_folder_assets(str(tmp_path), budget_cap=2)
    bd = r["by_dir"]
    assert bd["total"] == 4 and bd["truncated"] is True and bd["cap"] == 2
    assert len(bd["items"]) == 2
    assert bd["items"][0]["path"] == "/Game/D"  # 最大桶在前


def test_by_dir_cap_zero_returns_all(tmp_path):
    c = tmp_path / "Content"
    for name in ("A", "B", "C"):
        (c / name).mkdir(parents=True)
        with open(str(c / name / "x.uasset"), "wb") as f:
            f.write(b"\0" * 3)
    r = folderscan.scan_folder_assets(str(tmp_path), budget_cap=0)
    bd = r["by_dir"]
    assert bd["total"] == 3 and len(bd["items"]) == 3 and bd["truncated"] is False

