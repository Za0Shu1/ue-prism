"""list_level_actors：当前关卡 actor 清单（S2 数据底座）。真机经 bridge 执行。

unreal API 跨 5.0-5.8 有差异 -> 取 actor 列表/组件/标签均多候选 + best-effort，取不到不崩。
无引擎（总线测试）时降级返回空清单，结构键不变。
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


@register
def list_level_actors(class_contains=None, tag=None, limit=200):
    limit = int(limit)
    result = {
        "actors": [], "total": 0, "truncated": False, "cap": limit,
        "filters": {"class_contains": class_contains, "tag": tag},
    }
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
    for actor in actors_all:
        try:
            cls = actor.get_class().get_name()
        except Exception:
            cls = ""
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
    return result