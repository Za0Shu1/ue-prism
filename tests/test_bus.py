"""无引擎环境下的文件总线测试：BusClient(server侧) <-> bridge.handler 跑通 ping。

对应 docs/REQUIREMENTS.md §4.1。纯标准库，不需要 UE，也不需要 mcp SDK。
"""
from __future__ import annotations

import threading

from prism import bridge, bus, envelope


def _start_bridge(bdir):
    t = threading.Thread(target=bridge.run_forever, args=(bdir, 0.02), daemon=True)
    t.start()
    return t


def test_ping_roundtrip(tmp_path):
    bdir = str(tmp_path / "bus")
    _start_bridge(bdir)
    env = bus.BusClient(bdir, timeout=5).call("ping", {})
    assert env["ok"] is True
    assert env["result"]["bridge_alive"] is True


def test_list_level_actors_roundtrip(tmp_path):
    bdir = str(tmp_path / "bus")
    _start_bridge(bdir)
    env = bus.BusClient(bdir, timeout=5).call("list_level_actors", {"limit": 50})
    assert env["ok"] is True
    r = env["result"]
    assert set(["actors", "total", "truncated", "cap"]).issubset(r.keys())
    assert r["total"] == 0 and r["actors"] == []  # 无引擎降级
    assert r["cap"] == 50


def test_unknown_fn(tmp_path):
    bdir = str(tmp_path / "bus")
    _start_bridge(bdir)
    env = bus.BusClient(bdir, timeout=5).call("no_such_fn", {})
    assert env["ok"] is False
    assert env["error"]["code"] == envelope.Code.UNKNOWN_FN


def test_bridge_timeout(tmp_path):
    bdir = str(tmp_path / "empty")  # 故意不起 bridge
    env = bus.BusClient(bdir, timeout=0.5).call("ping", {})
    assert env["ok"] is False
    assert env["error"]["code"] == envelope.Code.BRIDGE_TIMEOUT