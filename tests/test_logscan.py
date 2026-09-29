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

# ---- P2: 按会话切分（日志未轮转时同一文件含多个 "Log file open"） ----

SAMPLE_MULTI = (
    "Log file open, 09/28/26 17:00:00\n"
    "[2024.05.01-03.35.12:345][  12]LogStreaming: Error: Failed to load resource 101\n"
    "[2024.05.01-03.35.12:400][  13]LogStreaming: Error: Failed to load resource 102\n"
    "[2024.05.01-03.35.13:000][  20]LogTemp: Warning: thing 1 is odd\n"
    "Log file open, 09/29/26 10:49:57\n"
    "[2024.05.01-04.00.00:000][   5]LogStreaming: Error: Failed to load resource 200\n"
    "[2024.05.01-04.00.00:100][   6]LogNet: Error: socket closed unexpectedly\n"
)


def test_multi_session_split(tmp_path):
    r = logscan.read_editor_log(_proj(tmp_path, SAMPLE_MULTI), level="Error")
    assert r["session_count"] == 2 and r["multi_session_in_tail"] is True
    assert r["note"]                       # 多会话时给提示，单会话时为 None
    byfp = {g["fingerprint"]: g for g in r["groups"]}
    shared = byfp["Failed to load resource #"]
    # 101/102 在会话1，200 在会话2 -> 跨两个会话、非本会话新增
    assert shared["count"] == 3
    assert shared["sessions"] == [1, 2] and shared["session_span"] == 2
    assert shared["in_latest_session"] is True and shared["new_in_latest_session"] is False
    only_new = byfp["socket closed unexpectedly"]
    # 仅出现在最新会话 -> 本会话新增（回归信号）
    assert only_new["sessions"] == [2] and only_new["new_in_latest_session"] is True


def test_sessions_level_tallies(tmp_path):
    r = logscan.read_editor_log(_proj(tmp_path, SAMPLE_MULTI), level="All")
    sess = {s["session_id"]: s for s in r["sessions"]}
    assert sess[1]["error"] == 2 and sess[1]["warning"] == 1
    assert sess[2]["error"] == 2
    assert all(s["lines"] > 0 for s in r["sessions"])   # 空的前置段已剔除


def test_single_session_not_multi(tmp_path):
    r = logscan.read_editor_log(_proj(tmp_path, SAMPLE), level="Error")
    assert r["session_count"] == 1 and r["multi_session_in_tail"] is False
    assert r["note"] is None

