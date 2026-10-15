"""场景合批分析：谁该进 HISM/ISM（v1.5 PR-1 只读 + PR-4 路线 B 方案输出）。

对应 v1.5 Feature 3。真实靶：某 5.3 工程单关卡 2096 Actor、2003 散装
StaticMeshActor、实例化占比 0%，>=5 重复同网格 49 组——纯 drawcall 白给。

铁律落位（D2 决策：路线 B 先行）：本模块**只出方案，不就地删/改关卡 Actor**。
分组判定与体量估算抽成纯函数 _group_rows，可离线测；关卡侧采集复用 actors.py
的探针（同网格识别、子关卡归属、已实例化宿主排除），口径与
list_level_actors(compose=True) 一致，不会出现两套数。
"""
from __future__ import annotations

from . import actors as _ac
from . import register

STATIC_MESH_ACTOR = "StaticMeshActor"


def _int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _group_rows(rows, min_count=5, include_samples=10, instanced_names=None):
    """[(mesh, class_name, level, actor_name)] -> 合批分组 + 排除统计（纯逻辑）。

    分组键 = 网格包路径（同网格 = 同材质槽布局，可共用一个 HISM）。
    风险标注：组内混入蓝图/非 StaticMeshActor 的 Actor -> 有逻辑/碰撞/交互的可能，
    合并前必须人工确认，故 blockers 单列而不是静默并入。
    """
    groups = {}
    excluded_instanced = 0
    excluded_no_mesh = 0
    plain_actors = 0
    instanced_names = instanced_names or ()
    for mesh, cls, level, name in rows:
        if not mesh:
            # 类名命中或组件命中（裸 Actor + HISM）都算已实例化宿主
            if name in instanced_names or (cls and ("Instanced" in cls
                                                    or "Foliage" in cls
                                                    or "Niagara" in cls)):
                excluded_instanced += 1
            else:
                excluded_no_mesh += 1
            continue
        plain_actors += 1
        slot = groups.setdefault(mesh, {"mesh": mesh, "actors": 0, "levels": {},
                                        "classes": {}, "samples": [],
                                        "non_staticmesh_actors": 0})
        slot["actors"] += 1
        lv = level or "?unknown_level"
        slot["levels"][lv] = slot["levels"].get(lv, 0) + 1
        cname = cls or "?"
        slot["classes"][cname] = slot["classes"].get(cname, 0) + 1
        if cname != STATIC_MESH_ACTOR:
            slot["non_staticmesh_actors"] += 1
        if len(slot["samples"]) < max(0, int(include_samples)):
            slot["samples"].append({"actor": name, "class": cname, "level": lv})
    merged = []
    for mesh, slot in groups.items():
        if slot["actors"] < int(min_count):
            continue
        merged.append({
            "mesh": mesh,
            "actors": slot["actors"],
            "levels": [{"level": k, "count": v} for k, v in
                       sorted(slot["levels"].items(), key=lambda kv: (-kv[1], kv[0]))],
            "actor_classes": [{"class": k, "count": v} for k, v in
                              sorted(slot["classes"].items(), key=lambda kv: (-kv[1], kv[0]))],
            "blockers": {"non_staticmesh_actors": slot["non_staticmesh_actors"]},
            "sample_actors": slot["samples"],
        })
    merged.sort(key=lambda g: (-g["actors"], g["mesh"]))
    collapsible = sum(g["actors"] - 1 for g in merged)
    risky = sum(1 for g in merged if g["blockers"]["non_staticmesh_actors"] > 0)
    return {
        "groups": merged,
        "groups_total": len(merged),
        "plain_staticmesh_actors": plain_actors,
        "collapsible_actors": collapsible,
        "drawcalls_before": sum(g["actors"] for g in merged),
        "drawcalls_after": len(merged),
        "groups_with_blockers": risky,
        "excluded_instanced_actors": excluded_instanced,
        "excluded_no_mesh_actors": excluded_no_mesh,
        "min_count": int(min_count),
    }


def _summary_only(summary):
    """去掉明细键的 summary（groups 只在外层出现一次，避免同一份数据两处膨胀）。"""
    out = dict(summary)
    out.pop("groups", None)
    return out


@register
def analyze_instancing(min_count=5, cap=40, include_samples=10, max_actors=5000,
                       class_contains=None, tag=None):
    """当前关卡的 HISM/ISM 合并方案（只读；不碰场景）。

    返回 groups（按 Actor 数降序，cap 截断）+ summary（可折叠 Actor 数、
    drawcall 前/后估算、含阻断项的组数）+ plan（路线 B 说明：要改场景需另走
    执行 PR，先提交 VCS / 另存关卡副本）+ total/truncated/cap。
    无引擎时结构键常驻、note=no_engine。
    """
    min_count = max(2, _int(min_count, 5))
    cap = max(1, _int(cap, 40))
    include_samples = _int(include_samples, 10)
    result = {
        "groups": [], "summary": _summary_only(_group_rows([], min_count, include_samples)),
        "filters": {"class_contains": class_contains, "tag": tag,
                    "max_actors": _int(max_actors, 5000)},
        "world_total_actors": 0, "scanned": 0,
        "total": 0, "truncated": False, "cap": cap,
        "note": None,
        "plan": {"strategy": "plan_only",
                 "note": ("路线 B（默认）：只出合并方案，不就地删/改关卡 Actor。"
                          "真要执行需另开写操作 PR，并且先提交 VCS 或另存关卡副本"
                          "——UE 对关卡 Actor 级改动没有事务回滚。")},
    }
    try:
        import unreal
    except Exception:
        result["note"] = "no_engine"
        return result

    actors_all = None
    for getter in (lambda: unreal.EditorLevelLibrary.get_all_level_actors(),
                   lambda: unreal.GameBaseLibrary.get_all_actors(
                       unreal.EditorLevelLibrary.get_editor_world())):
        try:
            value = getter()
        except Exception:
            continue
        if value is not None:
            actors_all = value
            break
    if actors_all is None:
        result["note"] = "取不到关卡 Actor（EditorLevelLibrary/LevelEditorSubsystem 签名漂移？编辑器里需已打开关卡）"
        return result

    limit = _int(max_actors, 5000)
    rows = []
    instanced_names = set()
    for actor in list(actors_all)[:limit]:
        try:
            cls = actor.get_class().get_name()
        except Exception:
            cls = ""
        if class_contains and str(class_contains).lower() not in str(cls).lower():
            continue
        if tag and not _ac._has_tag(actor, tag):
            continue
        mesh, inst = _ac._mesh_probe(unreal, actor, cls)
        name = actor.get_name()
        if inst:
            instanced_names.add(name)
        rows.append((mesh, cls, _ac._level_name(actor), name))

    summary = _group_rows(rows, min_count, include_samples,
                          instanced_names=instanced_names)
    groups = summary.pop("groups")
    result["world_total_actors"] = len(actors_all)
    result["scanned"] = len(rows)
    result["summary"] = summary
    result["total"] = len(groups)
    result["truncated"] = len(groups) > cap
    result["groups"] = groups[:cap]
    if not groups:
        result["note"] = "无 >= %d 重复的同网格散装 Actor（或关卡没打开/取不到）" % min_count
    elif summary["groups_with_blockers"]:
        result["note"] = ("%d 组内混有非 StaticMeshActor（蓝图放置器）的实例，"
                          "合并前需人工确认其逻辑/碰撞/交互，勿直接批量折叠。"
                          % summary["groups_with_blockers"])
    else:
        result["note"] = ("可折叠 %d 个散装 Actor 到 %d 个 HISM（drawcall 估算 %d -> %d）。"
                          "路线 B：本轮不动场景，执行需另开 PR + VCS 兜底。"
                          % (summary["collapsible_actors"], summary["drawcalls_after"],
                             summary["drawcalls_before"], summary["drawcalls_after"]))
    return result
