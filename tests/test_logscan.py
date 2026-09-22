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