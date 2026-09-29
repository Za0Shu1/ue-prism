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


def test_get_asset_references_classify_soft_degrade(tmp_path):
    """classify_soft=True 时无引擎仍返回稳定结构键（不假装成功，也不缺键）。"""
    bdir = str(tmp_path / "bus")
    _bridge(bdir)
    env = bus.BusClient(bdir, timeout=5).call(
        "get_asset_references",
        {"asset_path": "/Game/Foo/Bar.Bar", "direction": "used_by", "classify_soft": True},
    )
    assert env["ok"] is True
    r = env["result"]
    assert r["classify_soft"] is True
    assert set(["used_by_detail", "uses_detail", "used_by_soft_count", "uses_soft_count"]).issubset(r.keys())
    assert r["used_by_detail"] == [] and r["used_by_soft_count"] == 0


def test_merge_detail_hard_soft_union():
    from prism.domain import assets
    hard = {"/Game/A", "/Game/B"}
    soft = {"/Game/B", "/Game/C"}
    d = assets._merge_detail(hard, soft, limit=10)
    byp = {x["package"]: x for x in d}
    assert byp["/Game/A"] == {"package": "/Game/A", "hard": True, "soft": False}
    assert byp["/Game/B"]["hard"] is True and byp["/Game/B"]["soft"] is True
    assert byp["/Game/C"] == {"package": "/Game/C", "hard": False, "soft": True}
    assert [x["package"] for x in d] == ["/Game/A", "/Game/B", "/Game/C"]
    assert assets._merge_detail(hard, soft, limit=1) == [{"package": "/Game/A", "hard": True, "soft": False}]
    assert assets._merge_detail(None, None, 10) == []



def test_scan_broken_references_degrade(tmp_path):
    """无引擎：scan_broken_references 结构键常驻、返回空、不假装找到注册表。"""
    bdir = str(tmp_path / "bus")
    _bridge(bdir)
    env = bus.BusClient(bdir, timeout=5).call(
        "scan_broken_references",
        {"asset_paths": ["/Game/Foo/Bar"], "folder": "/Game", "limit": 10, "max_items": 10},
    )
    assert env["ok"] is True
    r = env["result"]
    for k in ("broken", "by_dep", "fixable_redirector_deps", "broken_count",
              "missing_count", "redirector_count", "assets_with_broken",
              "total_scanned", "total_available", "truncated", "cap", "scope"):
        assert k in r
    assert r["broken"] == [] and r["broken_count"] == 0
    assert r["missing_count"] == 0 and r["redirector_count"] == 0
    assert r["found_registry"] is False


def test_dep_resolution_status_kinds(monkeypatch):
    """_dep_resolution_status 三态判据：missing(解析不到)/redirector(桩)/ok(真资产)。"""
    from prism.domain import assets as A
    REDIR = object()
    OK = object()
    def fake_data(ar, unreal, pkg, obj):
        if pkg.endswith("/Missing"):
            return None
        if pkg.endswith("/Stub"):
            return REDIR
        return OK
    def fake_cls(unreal, data):
        if data is REDIR:
            return "ObjectRedirector"
        if data is OK:
            return "Texture2D"
        return None
    monkeypatch.setattr(A, "_asset_data", fake_data)
    monkeypatch.setattr(A, "_class_name", fake_cls)
    assert A._dep_resolution_status(None, None, "/Game/A/Missing") == ("missing", None)
    assert A._dep_resolution_status(None, None, "/Game/A/Stub") == ("redirector", "ObjectRedirector")
    assert A._dep_resolution_status(None, None, "/Game/A/Good") == ("ok", "Texture2D")
