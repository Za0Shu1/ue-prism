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