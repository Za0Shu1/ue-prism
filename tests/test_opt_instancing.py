"""v1.5 合批分析离线用例：分组/阻断/估算的纯逻辑契约 + 无引擎降级结构。

分组数学与 list_level_actors(compose=True) 同源（都认"同网格散装 StaticMeshActor"），
这里锁死：组阈值、可折叠数、drawcall 前/后、蓝图放置器只标不并。
"""
from __future__ import annotations

from prism.domain import opt_instancing as oi


def _rows(mesh, n, cls="StaticMeshActor", level="Persistent", start=0):
    return [(mesh, cls, level, "%s_%d" % (mesh.rpartition("/")[2], i))
            for i in range(start, start + n)]


def test_groups_by_mesh_with_collapsible_math():
    rows = _rows("/Game/Meshes/SM_Rock", 6) + _rows("/Game/Meshes/SM_Tree", 12)
    out = oi._group_rows(rows, min_count=5, include_samples=3)
    assert out["groups_total"] == 2
    assert out["plain_staticmesh_actors"] == 18
    assert [g["mesh"] for g in out["groups"]] == ["/Game/Meshes/SM_Tree", "/Game/Meshes/SM_Rock"]
    assert out["drawcalls_before"] == 18 and out["drawcalls_after"] == 2
    assert out["collapsible_actors"] == 16
    assert out["groups_with_blockers"] == 0
    assert out["min_count"] == 5
    assert len(out["groups"][0]["sample_actors"]) == 3     # 采样数受 include_samples 限制


def test_min_count_filters_small_repeats():
    out = oi._group_rows(_rows("/Game/Meshes/SM_Lamp", 4), min_count=5, include_samples=2)
    assert out["groups_total"] == 0 and out["collapsible_actors"] == 0


def test_blueprint_placed_actors_are_blockers_not_silently_merged():
    rows = (_rows("/Game/Meshes/SM_Door", 6, cls="StaticMeshActor")
            + _rows("/Game/Meshes/SM_Door", 2, cls="BP_Door_Blueprint", start=6))
    out = oi._group_rows(rows, min_count=5, include_samples=10)
    group = out["groups"][0]
    assert group["actors"] == 8
    assert group["blockers"]["non_staticmesh_actors"] == 2
    assert out["groups_with_blockers"] == 1
    classes = {c["class"]: c["count"] for c in group["actor_classes"]}
    assert classes == {"StaticMeshActor": 6, "BP_Door_Blueprint": 2}


def test_level_attribution_and_exclusions():
    rows = (_rows("/Game/Meshes/SM_Rock", 5, level="Persistent")
            + _rows("/Game/Meshes/SM_Rock", 3, level="Sub_Level_1", start=5)
            + [(None, "Foliage", "Persistent", "fol_1"),
               (None, "HierarchicalInstancedStaticMeshComponent", "Persistent", "hism_1"),
               (None, "StaticMeshActor", "Persistent", "empty_1")])
    out = oi._group_rows(rows, min_count=5, include_samples=2)
    assert out["groups_total"] == 1
    levels = {l["level"]: l["count"] for l in out["groups"][0]["levels"]}
    assert levels == {"Persistent": 5, "Sub_Level_1": 3}
    assert out["excluded_instanced_actors"] == 2   # Foliage + 已实例化宿主
    assert out["excluded_no_mesh_actors"] == 1     # 没挂网格的散装 Actor


def test_analyze_degrades_with_structure_keys():
    r = oi.analyze_instancing(min_count=6, cap=3)
    assert r["note"] == "no_engine"
    assert {"groups", "summary", "filters", "plan", "world_total_actors", "scanned",
            "total", "truncated", "cap", "note"} <= set(r)
    assert r["groups"] == [] and r["cap"] == 3 and r["summary"]["min_count"] == 6
    assert r["plan"]["strategy"] == "plan_only"      # D2 路线 B：只出方案不动场景
    assert "VCS" in r["plan"]["note"]


def test_analyze_min_count_floor_is_two():
    r = oi.analyze_instancing(min_count=1)
    assert r["summary"]["min_count"] == 2            # 1 个不构成"重复"


def test_summary_does_not_duplicate_groups():
    r = oi.analyze_instancing()
    assert "groups" not in r["summary"]              # 明细只在外层出现一次




def test_group_rows_counts_component_instancing_without_a_telling_class():
    """5.4 折叠容器是裸 Actor（类名里没 Instanced）：只能靠组件侧的名单认领。

    也锁住后向兼容：不传 instanced_names 时行为与早期一致，类名命中仍然算已实例化。
    """
    rows = [(None, "Actor", "P", "Actor_1"),
            (None, "FoliageInstancedStaticMeshActor", "P", "Foliage_2")]
    out = oi._group_rows(rows, min_count=5)
    assert out["excluded_instanced_actors"] == 1
    assert out["excluded_no_mesh_actors"] == 1
    out2 = oi._group_rows(rows, min_count=5, instanced_names=("Actor_1",))
    assert out2["excluded_instanced_actors"] == 2
    assert out2["excluded_no_mesh_actors"] == 0
    assert out2["plain_staticmesh_actors"] == 0
