"""scan_orphan_assets 无引擎降级 + 纯逻辑分类测试（不依赖 UE/mcp）。"""
from __future__ import annotations

import threading

from prism import bridge, bus


def _bridge(bdir):
    threading.Thread(target=bridge.run_forever, args=(bdir, 0.02), daemon=True).start()


def test_scan_orphan_degrade(tmp_path):
    bdir = str(tmp_path / "bus")
    _bridge(bdir)
    env = bus.BusClient(bdir, timeout=5).call("scan_orphan_assets", {"folder": "/Game"})
    assert env["ok"] is True
    r = env["result"]
    keys = {"folder", "orphan_count", "total_reclaimable_mb", "orphans", "by_dir", "excluded",             "confidence_counts", "cascade_enabled", "cascading_orphan_count",             "cascading_total_reclaimable_mb", "cascade_added_count", "cascade_nodes"}
    assert keys.issubset(r.keys())
    assert r["found_registry"] is False
    assert r["note"].startswith("no_engine")
    assert r["orphans"] == [] and r["orphan_count"] == 0
    assert r["cap"] == 2000 and r["offset"] == 0


def test_scan_orphan_cap_passthrough(tmp_path):
    bdir = str(tmp_path / "bus")
    _bridge(bdir)
    env = bus.BusClient(bdir, timeout=5).call(
        "scan_orphan_assets", {"folder": "Foo", "limit": 50, "offset": 100, "max_orphans": 5})
    r = env["result"]
    assert r["folder"] == "/Game/Foo"  # 非 /Game 前缀自动补齐
    assert r["cap"] == 50 and r["offset"] == 100


def test_orphan_exclusion_rules():
    from prism.domain import assets
    assert assets._orphan_exclusion("Texture2D", "/Game/A/T_x") is None
    assert assets._orphan_exclusion("World", "/Game/Maps/LV_Main") == "level_root"
    assert assets._orphan_exclusion("StaticMesh", "/Game/__ExternalActors__/Foo") == "external_wp"
    assert assets._orphan_exclusion("StaticMesh", "/Game/__ExternalObjects__/Foo") == "external_wp"
    assert assets._orphan_exclusion("Redirector", "/Game/A/Old") == "redirector"
    assert assets._orphan_exclusion("StaticMesh", "/Game/A/SM_Redirector1") == "redirector"
    assert assets._orphan_exclusion("Texture2D", "/Engine/BasicShapes/T") == "non_game"


def test_orphan_confidence_rules():
    from prism.domain import assets
    conf, reasons = assets._orphan_confidence("Texture2D", "/Game/A/T_clean", False, 999.0, 14)
    assert conf == "high" and reasons == []
    conf, reasons = assets._orphan_confidence("Texture2D", "/Game/A/T_pid", True, 999.0, 14)
    assert conf == "suspect" and any("primary" in x for x in reasons)
    conf, reasons = assets._orphan_confidence("Blueprint", "/Game/A/BP_x", False, 999.0, 14)
    assert conf == "suspect" and any("blueprint" in x for x in reasons)
    conf, reasons = assets._orphan_confidence("Texture2D", "/Game/Old/T_x", False, 999.0, 14)
    assert conf == "suspect" and any("scratch" in x or "editor" in x for x in reasons)
    conf, reasons = assets._orphan_confidence("Texture2D", "/Game/A/T_new", False, 2.0, 14)
    assert conf == "suspect" and any("recently_modified" in x for x in reasons)
    conf, reasons = assets._orphan_confidence("Texture2D", "/Game/_Hidden/T_x", False, 999.0, 14)
    assert conf == "suspect" and any("underscore" in x for x in reasons)
