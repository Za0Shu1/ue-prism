"""read_editor_log 离线解析测试（纯标准库，无引擎、无 mcp）。"""
from __future__ import annotations

import os

from prism import logscan

SAMPLE = (
    "[2024.05.01-03.35.12:345][  12]LogStreaming: Error: Failed to load resource 101\n"
    "[2024.05.01-03.35.12:400][  13]LogStreaming: Error: Failed to load resource 102\n"
    "[2024.05.01-03.35.13:000][  20]LogTemp: Warning: thing 1 is odd\n"
    "[2024.05.01-03.35.13:010][  21]LogTemp: Warning: thing 2 is odd\n"
    "[2024.05.01-03.35.13:020][ 22]LogInit: Display: normal line\n"
)


def _make_project(tmp_path):
    d = tmp_path / "Saved" / "Logs"
    os.makedirs(str(d))
    with open(str(d / "Proj.log"), "w", encoding="utf-8") as f:
        f.write(SAMPLE)
    return str(tmp_path)


def test_error_grouping_and_norm(tmp_path):
    r = logscan.read_editor_log(_make_project(tmp_path), level="Error")
    assert r["total_matched"] == 2
    assert r["distinct"] == 1          # 两条归一化到同一组（数字 -> #）
    g = r["groups"][0]
    assert g["count"] == 2
    assert g["category"] == "LogStreaming"
    assert g["first_seen"]


def test_freshness_fields(tmp_path):
    r = logscan.read_editor_log(_make_project(tmp_path), level="Error")
    assert r["source"] == "file" and r["realtime"] is False
    assert r["log_mtime"] and isinstance(r["age_seconds"], (int, float))


def test_warning(tmp_path):
    r = logscan.read_editor_log(_make_project(tmp_path), level="Warning")
    assert r["distinct"] == 1 and r["total_matched"] == 2


def test_all_and_cap(tmp_path):
    r = logscan.read_editor_log(_make_project(tmp_path), level="All")
    assert r["total_matched"] == 5
    assert r["truncated"] is False

# ---- P2: 日志指纹归一(路径/引号串降噪) + warning 归因到资产 ----

SAMPLE_P2 = (
    "[2024.05.01-03.35.12:345][  12]LogStreaming: Warning: Failed to read file '../../../Engine/X/Y_1.png' error.\n"
    "[2024.05.01-03.35.12:900][  18]LogStreaming: Warning: Failed to read file '../../../Engine/X/Z_2.png' error.\n"
    "[2024.05.01-03.35.13:010][  20]LogPhysics: Warning: TConvex Name:BodySetup /Game/ART/mod/plane008.plane008:BodySetup_0, Element [0] has no Geometry\n"
    "[2024.05.01-03.35.13:040][  24]LogPhysics: Warning: TConvex Name:BodySetup /Game/ART/mod/plane009.plane009:BodySetup_0, Element [0] has no Geometry\n"
)


def _proj(tmp_path, text):
    d = tmp_path / "Saved" / "Logs"
    os.makedirs(str(d))
    with open(str(d / "Proj.log"), "w", encoding="utf-8") as f:
        f.write(text)
    return str(tmp_path)


def test_path_fingerprint_merges_and_attribs(tmp_path):
    r = logscan.read_editor_log(_proj(tmp_path, SAMPLE_P2), level="Warning")
    bycat = {g["category"]: g for g in r["groups"]}
    # 只有资产/图片路径不同的同类告警 -> 归一到同一模板组（降噪）
    assert bycat["LogPhysics"]["count"] == 2 and bycat["LogStreaming"]["count"] == 2
    assert r["distinct"] == 2
    # 资产归因：/Game 包被登记到组
    ph = bycat["LogPhysics"]
    assert ph["asset_count"] == 2 and ph["assets_truncated"] is False
    assert any(a.startswith("/Game/ART/mod/plane") for a in ph["assets"])
    # 相对路径 ../../../Engine 非 /Game -> 不登记为资产（避免噪音归因）
    assert bycat["LogStreaming"]["asset_count"] == 0


def test_norm_masks_path_addr_number():
    n = logscan._norm(r"load /Game/A/B_3 fail at D:\C\D.uasset 0x1A2B frame 42")
    assert "/Game" not in n and ".uasset" not in n.lower() and "0x" not in n
    assert "<path>" in n and "<addr>" in n and "frame #" in n


def test_extract_assets_forms():
    got = logscan._extract_assets(r"pkg /Game/Foo/Bar_2 mesh D:\x\Content\A\Mesh.uasset cube /Engine/BasicShapes/Cube.C")
    assert any(t.startswith("/Game/Foo/Bar_2") for t in got)
    assert any(t.endswith("Mesh.uasset") for t in got)
    assert any(t.startswith("/Engine/BasicShapes/Cube") for t in got)
    assert logscan._extract_assets("/Game/A /Game/A /Game/A") == ["/Game/A"]   # 去重
