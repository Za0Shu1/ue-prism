"""describe_asset / get_asset_references 无引擎降级总线测试（结构稳定，不依赖 UE/mcp）。"""
from __future__ import annotations

import threading

from prism import bridge, bus


def _bridge(bdir):
    threading.Thread(target=bridge.run_forever, args=(bdir, 0.02), daemon=True).start()


def test_describe_asset_degrade(tmp_path):
    bdir = str(tmp_path / "bus")
    _bridge(bdir)
    env = bus.BusClient(bdir, timeout=5).call("describe_asset", {"asset_path": "/Game/Foo/Bar"})
    assert env["ok"] is True
    r = env["result"]
    assert set(["found", "query", "object_path", "class"]).issubset(r.keys())
    assert r["found"] is False and r["object_path"] == "/Game/Foo/Bar.Bar"  # 无引擎降级


def test_get_asset_references_degrade(tmp_path):
    bdir = str(tmp_path / "bus")
    _bridge(bdir)
    env = bus.BusClient(bdir, timeout=5).call(
        "get_asset_references", {"asset_path": "/Game/Foo/Bar.Bar", "direction": "uses", "limit": 10}
    )
    assert env["ok"] is True
    r = env["result"]
    assert set(["used_by", "uses", "direction", "truncated", "cap"]).issubset(r.keys())
    assert r["direction"] == "uses" and r["cap"] == 10
    assert r["uses"] == [] and r["used_by"] == []  # 无引擎降级


def test_normalize_variants():
    from prism.domain import assets
    assert assets._normalize("/Game/A/B") == ("/Game/A/B", "/Game/A/B.B", "B")
    assert assets._normalize("/Game/A/B.B") == ("/Game/A/B", "/Game/A/B.B", "B")
    assert assets._normalize("/Game/A/B.B:StaticMeshComponent0") == ("/Game/A/B", "/Game/A/B.B", "B")


def test_find_cycles_real_loops_vs_dag_crossedge():
    from prism.domain import assets as A
    # DAG 交叉边（A、B 共依赖 C）不是环，不许误报
    dag = {"/Game/R": ["/Game/A", "/Game/B"], "/Game/A": ["/Game/C"],
           "/Game/B": ["/Game/C", "/Game/D"], "/Game/C": [], "/Game/D": []}
    cyc, trunc = A._find_cycles(dag, ["/Game/A", "/Game/B", "/Game/C", "/Game/D"], "/Game/R")
    assert cyc == [] and trunc is False
    # 真回边 D->R：{R,B,D} 同 SCC；D->A 交叉边不产生第二个假环
    ent = {"/Game/R": ["/Game/B"], "/Game/B": ["/Game/D"],
           "/Game/D": ["/Game/R", "/Game/A"], "/Game/A": []}
    cyc, _ = A._find_cycles(ent, ["/Game/B", "/Game/D", "/Game/A"], "/Game/R")
    assert len(cyc) == 1 and cyc[0]["size"] == 3 and cyc[0]["contains_root"] is True
    assert cyc[0]["members"] == ["/Game/B", "/Game/D", "/Game/R"]


def test_find_cycles_self_loop_and_cap():
    from prism.domain import assets as A
    cyc, _ = A._find_cycles({"/Game/R": ["/Game/R"]}, [], "/Game/R")
    assert len(cyc) == 1 and cyc[0]["self_loop"] is True and cyc[0]["size"] == 1
    two = {"/Game/R": ["/Game/A", "/Game/C"], "/Game/A": ["/Game/B"], "/Game/B": ["/Game/A"],
           "/Game/C": ["/Game/D"], "/Game/D": ["/Game/C"]}
    cyc, trunc = A._find_cycles(two, ["/Game/A", "/Game/B", "/Game/C", "/Game/D"], "/Game/R", cap=1)
    assert len(cyc) == 1 and trunc is True
    assert cyc[0]["contains_root"] is False


def test_asset_chain_hardening_keys_via_bus(tmp_path):
    # 无引擎总线降级：新字段结构齐、类型稳定（envelope 契约不因加固而破坏）
    bdir = str(tmp_path / "bus")
    _bridge(bdir)
    env = bus.BusClient(bdir, timeout=5).call(
        "get_asset_chain", {"asset_path": "/Game/Foo/Bar", "direction": "used_by", "god_min_refs": 10})
    assert env["ok"] is True
    r = env["result"]
    for k in ("cycles", "cyclic", "cycles_truncated", "god_assets", "god_scan", "impact_summary"):
        assert k in r
    assert r["cycles"] == [] and r["cyclic"] is False
