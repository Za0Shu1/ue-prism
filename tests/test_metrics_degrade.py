"""PR-B get_asset_metrics 无引擎降级总线测试（结构稳定、note 明示，不依赖 UE/mcp）。"""
from __future__ import annotations

import threading

from prism import bridge, bus


def test_get_asset_metrics_degrade(tmp_path):
    b = str(tmp_path / "bus")
    threading.Thread(target=bridge.run_forever, args=(b, 0.02), daemon=True).start()
    env = bus.BusClient(b, timeout=5).call(
        "get_asset_metrics", {"asset_paths": ["/Game/A/B.B", "/Game/C"], "max_assets": 20})
    assert env["ok"] is True
    r = env["result"]
    assert r["requested"] == 2 and r["count"] == 2 and r["truncated"] is False
    assert all(i.get("note") == "no_engine" for i in r["items"])


def test_get_asset_metrics_cap(tmp_path):
    b = str(tmp_path / "bus")
    threading.Thread(target=bridge.run_forever, args=(b, 0.02), daemon=True).start()
    paths = ["/Game/X%d" % i for i in range(25)]
    env = bus.BusClient(b, timeout=5).call("get_asset_metrics", {"asset_paths": paths, "max_assets": 10})
    r = env["result"]
    assert r["count"] == 10 and r["truncated"] is True and r["requested"] == 25 and r["cap"] == 10


def test_get_asset_metrics_string_separators(tmp_path):
    """Regression: a comma/semicolon-joined string must split into clean paths,
    not explode char by char (list(str) bug)."""
    b = str(tmp_path / "bus")
    threading.Thread(target=bridge.run_forever, args=(b, 0.02), daemon=True).start()
    env = bus.BusClient(b, timeout=5).call(
        "get_asset_metrics",
        {"asset_paths": "/Game/A, /Game/B;/Game/C ,/Game/D", "max_assets": 20})
    assert env["ok"] is True
    r = env["result"]
    assert r["requested"] == 4 and r["count"] == 4 and r["truncated"] is False
    assert all(i.get("note") == "no_engine" for i in r["items"])
