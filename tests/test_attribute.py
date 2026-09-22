"""PR-3 归因测试：指纹分组/磁盘解析/工具接线/批量总线函数/在线富化（全离线或降级桥）。"""
from __future__ import annotations

import json
import os
import threading
import time

from prism import attributelog, bridge, bus, envelope, server, tasks

SAMPLE = (
    "LogInit: Engine Version: 5.4.4\n"
    "[2026.01.01-00.00.01:000][  0]LogCook: Error: Failed to save package /Game/Bad/Mesh.Mesh - missing shader\n"
    "[2026.01.01-00.00.02:000][  0]LogCook: Error: Failed to save package /Game/Bad/Mesh2.Mesh2 - missing shader\n"
    "[2026.01.01-00.00.03:000][  0]LogWorldPartition: Error: Could not find package /Game/Missing/Thing referenced by NewMap\n"
    "[2026.01.01-00.00.04:000][  0]LogCook: Display: all good\n"
    "ERROR: Command failed with exit code 42\n"
    "Fatal error: /Game/Critical/Asset crashed hard\n"
)


def _mk_project(tmp_path):
    proj = tmp_path / "Proj"
    (proj / "Content" / "Bad").mkdir(parents=True)
    (proj / "Content" / "Bad" / "Mesh.uasset").write_bytes(b"x" * 1234)
    log = tmp_path / "cook.log"
    log.write_text(SAMPLE, encoding="utf-8")
    return str(proj), str(log)


def _cfg(tmp_path, proj, log):
    bus_dir = tmp_path / "Prism"
    bus_dir.mkdir(exist_ok=True)
    server._CFG["project_dir"] = proj
    server._CFG["bus_dir"] = str(bus_dir)
    return str(bus_dir)


def test_parse_log_grouping(tmp_path):
    proj, log = _mk_project(tmp_path)
    rep = attributelog.parse_log(log, project_dir=proj)
    assert rep["ok" if False else "scanned_lines"] == 7
    assert rep["error_lines"] == 5
    assert rep["total"] == 4 and rep["truncated"] is False
    top = rep["patterns"][0]
    assert top["count"] == 2 and "<ASSET>" in top["pattern"] and "-" not in top["pattern"][:1]
    by_path = {a["path"]: a for g in rep["patterns"] for a in g["assets"]}
    assert by_path["/Game/Bad/Mesh"]["exists"] is True
    assert by_path["/Game/Bad/Mesh"]["size_bytes"] == 1234
    assert by_path["/Game/Bad/Mesh2"]["exists"] is False
    assert by_path["/Game/Critical/Asset"]["exists"] is False
    assert rep["enriched"] is False


def test_parse_top_cap(tmp_path):
    proj, log = _mk_project(tmp_path)
    rep = attributelog.parse_log(log, project_dir=proj, top=2)
    assert len(rep["patterns"]) == 2 and rep["truncated"] is True and rep["cap"] == 2


def test_many_assets_group_cap(tmp_path):
    lines = "".join(
        "[t][ 0]LogCook: Error: broken /Game/Bad/A%d.uasset\n" % i for i in range(12))
    log = tmp_path / "big.log"
    log.write_text(lines, encoding="utf-8")
    rep = attributelog.parse_log(str(log), project_dir=str(tmp_path))
    g = rep["patterns"][0]
    assert g["count"] == 12 and len(g["assets"]) == attributelog.PER_GROUP_CAP
    assert g["assets_truncated"] is True and g["assets_total"] == 12


def test_tool_by_log_path(tmp_path, monkeypatch):
    monkeypatch.delenv("PRISM_ENGINE_ROOT", raising=False)
    proj, log = _mk_project(tmp_path)
    _cfg(tmp_path, proj, log)
    try:
        env = server.attribute_cook_errors(log_path=log)
        assert env["ok"] is True
        r = env["result"]
        assert r["total"] == 4 and r["error_lines"] == 5
        assert r["enriched"] is False and "offline" in r["enrich_note"]
    finally:
        server._CFG["project_dir"] = None
        server._CFG["bus_dir"] = None


def test_tool_requires_input(tmp_path):
    env = server.attribute_cook_errors()
    assert env["ok"] is False and env["error"]["code"] == envelope.Code.RUNTIME_ERROR


def test_tool_by_task_id(tmp_path):
    proj, log = _mk_project(tmp_path)
    b = _cfg(tmp_path, proj, log)
    try:
        rec = tasks.create(b, "cook", proj, "cmd", log)
        env = server.attribute_cook_errors(task_id=rec["task_id"])
        assert env["ok"] and env["result"]["error_lines"] == 5
        bad = server.attribute_cook_errors(task_id="cook-nope-0000")
        assert bad["ok"] is False and bad["error"]["code"] == envelope.Code.TASK_NOT_FOUND
    finally:
        server._CFG["project_dir"] = None
        server._CFG["bus_dir"] = None


def _bridge_thread(b):
    threading.Thread(target=bridge.run_forever, args=(b, 0.02), daemon=True).start()


def test_domain_batch_roundtrip(tmp_path):
    b = str(tmp_path / "bus")
    _bridge_thread(b)
    c = bus.BusClient(b, timeout=5)
    d = c.call("describe_many", {"paths": ["/Game/A", "/Game/B"]})
    assert d["ok"] is True
    assert d["result"]["count"] == 2 and d["result"]["requested"] == 2
    assert d["result"]["items"][0]["query"] == "/Game/A" and d["result"]["items"][0]["found"] is False
    r = c.call("referencers_many", {"paths": ["/Game/A"], "cap": 5})
    assert r["ok"] and r["result"]["items"][0]["package_name"] == "/Game/A"
    assert r["result"]["items"][0]["used_by_count"] == 0  # 无引擎降级


def test_enrichment_when_bridge_live(tmp_path):
    proj, log = _mk_project(tmp_path)
    b = _cfg(tmp_path, proj, log)
    # 先落新鲜心跳再起降级桥（避免 write_heartbeat 被限频跳过 / 线程首帧竞态）
    bus.write_heartbeat(b)
    _bridge_thread(b)
    _deadline = time.time() + 2.0  # 等首帧心跳确实在盘上，再断言在线富化路径
    while bus.heartbeat_age(b) is None and time.time() < _deadline:
        time.sleep(0.02)
    assert bus.heartbeat_age(b) is not None
    try:
        env = server.attribute_cook_errors(log_path=log)
        assert env["ok"] and env["result"]["error_lines"] == 5
        r = env["result"]
        assert r["enriched"] is True and "enriched" in r["enrich_note"]
        mesh = [a for g in r["patterns"] for a in g["assets"] if a["path"] == "/Game/Bad/Mesh"][0]
        assert mesh["used_by_count"] == 0  # 降级桥的批量回包
    finally:
        server._CFG["project_dir"] = None
        server._CFG["bus_dir"] = None
