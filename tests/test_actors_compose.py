"""P1-2 场景构成聚合测试：_build_composition 纯逻辑 + 无引擎降级总线（结构键常驻）。"""
from __future__ import annotations

import threading

from prism import bridge, bus
from prism.domain import actors as A


def _row(cls, level="L1", mesh=None, inst=False):
    return (cls, level, mesh, inst)


def test_class_level_tally_sorted_and_capped():
    rows = [_row("StaticMeshActor")] * 10 + [_row("Light")] * 3 + [_row("CameraActor", level="Sub2")] * 2
    c = A._build_composition(rows, 5, 2, 15)
    assert c["by_class"]["items"][0] == {"name": "StaticMeshActor", "count": 10}
    assert c["by_class"]["total"] == 3 and c["by_class"]["truncated"] is True
    assert c["by_level"]["total"] == 2 and c["by_level"]["truncated"] is False
    assert c["world_total_actors"] == 15


def test_instancing_opportunities_threshold_and_exclusions():
    rows = [_row("StaticMeshActor", mesh="/Game/M/Demo%d" % i) for i in range(3)]
    rows += [_row("StaticMeshActor", mesh="/Game/M/Repeated")] * 12
    rows += [_row("StaticMeshActor", mesh="/Game/M/Almost")] * 4
    rows += [_row("StaticMeshActor", mesh="/Game/M/Ism", inst=True)] * 50  # 已 ISM 合批不算机会
    c = A._build_composition(rows, 5, 30, len(rows))
    opps = c["instancing_opportunities"]
    assert opps == [{"mesh": "/Game/M/Repeated", "actors": 12}], opps
    assert c["instanced_actor_count"] == 50
    assert c["plain_staticmesh_actors"] == 19
    assert c["instanced_share_pct"] == round(100.0 * 50 / 69, 2)


def test_missing_level_bucketed_not_dropped():
    rows = [_row("StaticMeshActor", level=None)] * 3
    c = A._build_composition(rows, 5, 30, 3)
    assert c["by_level"]["items"][0]["name"] == "?unknown_level"


def test_opportunities_truncated_flag():
    rows = []
    for i in range(5):
        rows += [_row("StaticMeshActor", mesh="/Game/M/Rep%d" % i)] * 6
    c = A._build_composition(rows, 5, 3, 30)
    assert c["opportunities_total"] == 5 and c["opportunities_truncated"] is True
    assert len(c["instancing_opportunities"]) == 3


def test_degrade_bus_compose_keys_present():
    """无引擎：compose=True 也必须返回完整结构键（降级不崩、键位不缺席）。"""
    import tempfile, os
    with tempfile.TemporaryDirectory() as tmp:
        b = os.path.join(tmp, "bus")
        threading.Thread(target=bridge.run_forever, args=(b, 0.02), daemon=True).start()
        env = bus.BusClient(b, timeout=5).call(
            "list_level_actors", {"limit": 10, "compose": True,
                                  "dup_min_count": 7, "composition_cap": 4})
        assert env["ok"] is True
        r = env["result"]
        comp = r["composition"]
        assert comp["dup_min_count"] == 7 and comp["cap"] == 4
        assert comp["by_class"]["total"] == 0 and comp["instancing_opportunities"] == []
        assert comp["world_total_actors"] == 0
        env2 = bus.BusClient(b, timeout=5).call("list_level_actors", {"limit": 10})
        assert "composition" not in env2["result"]  # 默认关闭，规则通道行为不变
