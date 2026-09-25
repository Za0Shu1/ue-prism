"""list_level_actors：当前关卡 actor 清单 + 场景构成诊断（S2 数据底座）。真机经 bridge 执行。

unreal API 跨 5.0-5.8 有差异 -> 取 actor 列表/组件/网格/子关卡均多候选 + best-effort，取不到不崩。
无引擎（总线测试）时降级返回空清单，结构键不变。

compose=True 追加构成聚合（P1）：按类/按子关卡(streaming)计数 + 重复 StaticMesh
未实例化计数（HISM/ISM 合批机会清单，dup_min_count 阈值）；组件级识别 ISM/Foliage
宿主，避免把已合批 actor 误计入机会清单。
"""
from __future__ import annotations

from . import register


def _has_tag(actor, tag):
    try:
        return any(str(t).lower() == str(tag).lower() for t in actor.tags)
    except Exception:
        return False


def _count_components(unreal, actor):
    """组件数：5.4 上 actor.get_components() 不可用，改试多种签名，全失败返回 None。"""
    candidates = (
        lambda: actor.get_components_by_class(unreal.ActorComponent),
        lambda: unreal.SystemLibrary.get_components_by_class(actor, unreal.ActorComponent),
        lambda: actor.components,
        lambda: actor.get_components(),
    )
    for getter in candidates:
        try:
            comps = getter()
        except Exception:
            continue
        if comps is not None:
            try:
                return len(comps)
            except Exception:
                return None
    return None


def _level_name(actor):
    """所属子关卡名（streaming 分组预算用）；取不到返回 None。"""
    for getter in (lambda: actor.get_level().get_name(),
                   lambda: actor.get_editor_property("level").get_name()):
        try:
            v = getter()
        except Exception:
            continue
        if v:
            return str(v)
    return None


def _comp_class(comp):
    try:
        return str(comp.get_class().get_name())
    except Exception:
        return ""


def _is_instanced_comp(comp):
    """ISM/HISM/Foliage/Niagara 等合批宿主组件（其 actor 不该进"未实例化"清单）。"""
    cls = _comp_class(comp)
    return ("Instanced" in cls or "Foliage" in cls or "Niagara" in cls)


def _mesh_probe(unreal, actor, cls):
    """返回 (mesh_package_path 或 None, instanced 布尔)。只认 StaticMesh 系 actor。"""
    if "StaticMesh" not in cls and "SkeletalMesh" not in cls:
        return None, False
    for getter in (lambda: actor.get_components_by_class(unreal.StaticMeshComponent),
                   lambda: unreal.SystemLibrary.get_components_by_class(
                       actor, unreal.StaticMeshComponent)):
        try:
            comps = getter()
        except Exception:
            continue
        for comp in (comps or []):
            if _is_instanced_comp(comp):
                return None, True
            mesh = None
            for mgetter in (lambda: comp.static_mesh,
                            lambda: comp.get_editor_property("static_mesh")):
                try:
                    mesh = mgetter()
                except Exception:
                    continue
                if mesh is not None:
                    break
            if mesh is None:
                continue
            try:
                s = str(mesh.get_path_name() or "")
            except Exception:
                return None, False
            if not s or s.startswith("Default__"):
                return None, False
            # /Game/Foo/Bar.Bar -> /Game/Foo/Bar
            return s.split(".")[0], False
    return None, False


def _build_composition(rows, dup_min_count, cap, world_total):
    """纯逻辑（无引擎可测）：[(class, level, mesh, instanced)] -> 构成摘要 + 合批机会。

    - by_class/by_level：数量降序 + total/truncated/cap 纪律；
    - instancing_opportunities：同一网格被 >= dup_min_count 个普通 StaticMeshActor
      平铺放置（非 ISM/HISM 宿主）= HISM/ISM 合批机会，按 actor 数降序；
    - instanced_actor_count/share：已走实例化合批的现状。
    """
    by_class = {}
    by_level = {}
    mesh_plain = {}
    instanced = 0
    for cls, level, mesh, inst in rows:
        by_class[cls] = by_class.get(cls, 0) + 1
        lv = level or "?unknown_level"
        by_level[lv] = by_level.get(lv, 0) + 1
        if inst:
            instanced += 1
        elif mesh:
            mesh_plain[mesh] = mesh_plain.get(mesh, 0) + 1

    def _top(counter):
        items = [{"name": k, "count": v} for k, v in counter.items()]
        items.sort(key=lambda r: (-r["count"], r["name"]))
        return {"items": items[:cap], "total": len(items), "truncated": len(items) > cap}

    opp = [{"mesh": k, "actors": v} for k, v in mesh_plain.items() if v >= dup_min_count]
    opp.sort(key=lambda r: (-r["actors"], r["mesh"]))
    total_rows = len(rows) or 1
    return {
        "by_class": _top(by_class),
        "by_level": _top(by_level),
        "instanced_actor_count": instanced,
        "instanced_share_pct": round(100.0 * instanced / total_rows, 2),
        "plain_staticmesh_varieties": len(mesh_plain),
        "plain_staticmesh_actors": sum(mesh_plain.values()),
        "instancing_opportunities": opp[:cap],
        "opportunities_total": len(opp),
        "opportunities_truncated": len(opp) > cap,
        "dup_min_count": int(dup_min_count),
        "cap": int(cap),
        "world_total_actors": world_total,
    }


@register
def list_level_actors(class_contains=None, tag=None, limit=200, compose=False,
                      dup_min_count=5, composition_cap=30):
    limit = int(limit)
    result = {
        "actors": [], "total": 0, "truncated": False, "cap": limit,
        "filters": {"class_contains": class_contains, "tag": tag},
    }
    compose = bool(compose)
    if compose:
        # 降级路径键位常驻：无引擎时结构不变（空摘要）
        result["composition"] = _build_composition([], dup_min_count, composition_cap, 0)
    try:
        import unreal  # 惰性：无引擎环境走降级返回
    except Exception:
        return result

    actors_all = None
    for getter in (
        lambda: unreal.EditorLevelLibrary.get_all_level_actors(),
        lambda: unreal.GameBaseLibrary.get_all_actors(unreal.EditorLevelLibrary.get_editor_world()),
    ):
        try:
            value = getter()
        except Exception:
            continue
        if value is not None:
            actors_all = value
            break
    if actors_all is None:
        return result

    matched = []
    rows = [] if compose else None
    for actor in actors_all:
        try:
            cls = actor.get_class().get_name()
        except Exception:
            cls = ""
        if compose:
            mesh, inst = _mesh_probe(unreal, actor, cls)
            rows.append((cls, _level_name(actor), mesh, inst))
        if class_contains and str(class_contains).lower() not in cls.lower():
            continue
        if tag and not _has_tag(actor, tag):
            continue
        entry = {"name": actor.get_name(), "class": cls}
        try:
            entry["label"] = actor.get_actor_label()
        except Exception:
            pass
        try:
            entry["path"] = actor.get_path_name()
        except Exception:
            pass
        try:
            loc = actor.get_actor_location()
            entry["location"] = [loc.x, loc.y, loc.z]
        except Exception:
            pass
        entry["components"] = _count_components(unreal, actor)
        matched.append(entry)

    total = len(matched)
    result["total"] = total
    result["truncated"] = total > limit
    result["actors"] = matched[:limit]
    result["world_total_actors"] = len(actors_all)
    if compose:
        result["composition"] = _build_composition(rows, dup_min_count,
                                                   composition_cap, len(actors_all))
    return result
