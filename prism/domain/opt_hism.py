"""HISM 折叠执行器（路线 A：真改场景 + 自带回滚清单，v1.5 PR-5）。

analyze_instancing 只出方案（路线 B）；本模块把方案落成动作：同网格的散装
StaticMeshActor -> 一个 HierarchicalStaticMeshActor，源 Actor 的世界变换进实例，
源 Actor 销毁。UE 对关卡 Actor 级改动没有事务回滚，所以折叠前把每个源 Actor 的
类/网格/变换/标签/子关卡落一份 JSON 清单，mode="restore" 据此重建。

铁律落位：默认 dry_run（plan），落盘需 dry_run=False 且 confirm=True 双钥；
组内混有非 StaticMeshActor（蓝图放置器）默认拒收；源 Actor 带材质覆盖时折叠会
丢覆盖，默认同样拒收——两类都要显式 allow_* 才动，拒收原因如实回给调用方。
"""
from __future__ import annotations

import json
import os
import time

from .. import envelope
from . import actors as _ac
from . import opt_instancing as _oi
from . import optutil as _u
from . import register

OP = "opt_collapse_hism"
# 折叠容器：5.4 真机没有 AHierarchicalStaticMeshActor 这个 Actor 类
# （UnrealEditor-Engine.dll 的名字表里查无此类），引擎自己的 Instancing 工具走的是
# "裸 AActor + InstancedStaticMesh 组件"。所以先按专用 Actor 类试（兼容真有该类的
# 版本），拿不到就退回 5.4 实证路线。
HISM_CLASS = "HierarchicalStaticMeshActor"
_HISM_ACTOR_CLASS_NAMES = ("HierarchicalStaticMeshActor", "InstancedStaticMeshActor")
_HISM_COMP_CLASS_NAMES = ("HierarchicalInstancedStaticMeshComponent",
                          "HierarchicalStaticMeshComponent")
_HISM_COMP_NAME = "PrismHISMComponent"


def _mesh_stem(package_path):
    """网格包路径 -> Actor 标签用的短名（/Engine/BasicShapes/Cube.Cube -> Cube）。"""
    text = str(package_path or "")
    name = text.rsplit("/", 1)[-1]
    name = name.split(".")[-1]
    return name or "HISM"
STATIC_MESH_ACTOR = _oi.STATIC_MESH_ACTOR
MANIFEST_VERSION = 1
_ORIGIN = (0.0, 0.0, 0.0)

# 引擎隐式默认材质（包裹路径）：出现在覆盖数组里不代表作者改过材质。
# 网格材质槽声明为 None 时，组件解析值与数组预填值代入的就是这一族，
# 比路径时把它们视作同一个默认外观，否则整组散装网格会被误判成有覆盖。
_ENGINE_DEFAULT_MATERIALS = frozenset([
    "/Engine/EngineMaterials/WorldGridMaterial",
    "/Engine/BasicShapes/BasicShapeMaterial",
    "/Engine/EngineMaterials/DefaultMaterial",
    "/Engine/EngineMaterials/InvalidMaterial",
])


def _int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _base(mode, dry_run):
    return {"op": OP, "mode": str(mode or "plan"), "dry_run": _u.to_bool(dry_run, True),
            "executed": False, "success": False, "changed": 0, "no_op": 0,
            "planned": 0, "skipped": [], "failed": [], "rejected": 0,
            "groups": [], "refused_groups": [], "hism_created": 0,
            "actors_instanced": 0, "actors_destroyed": 0,
            "blockers_refused": 0, "material_override_refused": 0,
            "material_override_unknown": 0,
            "drawcalls_before": 0, "drawcalls_after": 0,
            "universe": 0, "scanned": 0, "total": 0, "truncated": False,
            "cap": 0, "note": None, "tried": [], "level_saved": False,
            "scope": {"mode": str(mode or "plan")},
            "manifest_path": None, "rollback_available": False,
            "restore_mesh_missing": 0, "failed_truncated": False}


def _fail(result, row, cap=20):
    """failed \u660e\u7ec6\u6709\u4e0a\u9650\uff1a1500 \u4e2a\u540c\u7c7b\u9519\u8bef\u4e0d\u8be5\u628a\u4fe1\u5c01\u6491\u7206\uff0c\u4f46\u8ba1\u6570\u7167\u5e38\u8d70\u3002"""
    if len(result["failed"]) < cap:
        result["failed"].append(row)
    else:
        result["failed_truncated"] = True

def _select_groups(rows, mesh=None, min_count=5, limit=20,
                   allow_blockers=False, allow_material_overrides=False,
                   include_samples=0):
    """纯逻辑：方案行 -> 本轮真要折叠的组 + 拒收原因（无引擎依赖，离线可测）。

    rows = [(mesh, cls, level, actor_name, has_material_override)]；分组口径与
    analyze_instancing 同源（都走 _oi._group_rows），两套数不会打架。
    """
    plain = []
    overrides = {}
    for row in rows:
        mesh_path, cls, level, name = row[0], row[1], row[2], row[3]
        plain.append((mesh_path, cls, level, name))
        if len(row) > 4 and row[4]:
            overrides[str(mesh_path)] = overrides.get(str(mesh_path), 0) + 1
    summary = _oi._group_rows(plain, min_count=_int(min_count, 5),
                              include_samples=_int(include_samples, 0))
    wanted = str(mesh or "").strip().lower()
    chosen = []
    refused = []
    for group in summary["groups"]:
        name = str(group["mesh"] or "")
        if wanted and wanted not in name.lower():
            continue
        item = dict(group)
        item["material_overrides"] = int(overrides.get(name, 0))
        why = None
        if item["blockers"]["non_staticmesh_actors"] and not _u.to_bool(allow_blockers, False):
            why = "blockers"
        elif item["material_overrides"] and not _u.to_bool(allow_material_overrides, False):
            why = "material_overrides"
        if why:
            item["refused"] = why
            refused.append(item)
        else:
            item["refused"] = None
            chosen.append(item)
    want_limit = max(1, _int(limit, 20))
    picked = chosen[:want_limit]
    for extra in chosen[want_limit:]:
        item = dict(extra)
        item["refused"] = "over_limit"
        refused.append(item)
    return {
        "groups": picked,
        "refused": refused,
        "collapsible_groups": len(chosen),
        "blockers_refused": sum(1 for g in refused if g["refused"] == "blockers"),
        "material_override_refused": sum(
            1 for g in refused if g["refused"] == "material_overrides"),
        "drawcalls_before": sum(_int(g["actors"], 0) for g in picked),
        "drawcalls_after": len(picked),
        "collapsible_actors": sum(max(0, _int(g["actors"], 0) - 1) for g in picked),
        "min_count": _int(min_count, 5),
        "summary": summary,
    }


def _write_manifest(path, payload):
    """原子写（.tmp + os.replace，同 bus 口径）：半截清单等于没有回滚。"""
    folder = os.path.dirname(str(path))
    if folder and not os.path.isdir(folder):
        os.makedirs(folder)
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=1)
    os.replace(tmp, str(path))
    return str(path)


def _read_manifest(path):
    try:
        with open(str(path), "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except Exception:
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("groups"), list):
        return None
    return payload


def _u_project_dir(unreal):
    for getter in (lambda: unreal.SystemLibrary.get_project_directory(),
                   lambda: unreal.Paths.project_dir()):
        try:
            value = getter()
        except Exception:
            continue
        if value:
            return str(value)
    return ""


def _manifest_path(unreal, given):
    if given:
        return str(given)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    root = ""
    for getter in (lambda: unreal.SystemLibrary.get_project_saved_directory(),
                   lambda: os.path.join(_u_project_dir(unreal), "Saved")):
        try:
            value = getter()
        except Exception:
            value = None
        if value:
            root = str(value).rstrip("/\\")
            break
    if not root:
        root = os.path.join(os.getcwd(), "Saved")
    return os.path.join(root, "Prism", "hism_collapse_%s.json" % stamp)


def _ue_version(unreal):
    for getter in (lambda: str(unreal.BuildVersion()),
                   lambda: str(unreal.EngineVersion)):
        try:
            value = getter()
        except Exception:
            continue
        if value:
            return value
    return ""


def _editor_world(unreal, tried):
    for label, call in (("EditorLevelLibrary.get_editor_world",
                         lambda: unreal.EditorLevelLibrary.get_editor_world()),
                        ("LevelEditorSubsystem.get_editor_world",
                         lambda: unreal.get_editor_subsystem(
                             unreal.LevelEditorSubsystem).get_editor_world())):
        try:
            world = call()
        except Exception as exc:
            tried.append("%s:%s" % (label, type(exc).__name__))
            continue
        if world is not None:
            tried.append(label)
            return world
    tried.append("world:miss")
    return None


def _all_actors(unreal, tried):
    for label, call in (("EditorLevelLibrary.get_all_level_actors",
                         lambda: unreal.EditorLevelLibrary.get_all_level_actors()),
                        ("EditorActorSubsystem.get_all_level_actors",
                         lambda: unreal.get_editor_subsystem(
                             unreal.EditorActorSubsystem).get_all_level_actors())):
        try:
            value = call()
        except Exception as exc:
            tried.append("%s:%s" % (label, type(exc).__name__))
            continue
        if value is not None:
            tried.append(label)
            return list(value)
    tried.append("get_all_level_actors:miss")
    return None


def _to_list(vec):
    out = []
    for key in ("x", "y", "z"):
        value = getattr(vec, key, None)
        try:
            out.append(float(value))
        except (TypeError, ValueError):
            out.append(0.0)
    return out


def _mesh_component(unreal, actor):
    for call in (lambda: actor.get_component_by_class(unreal.StaticMeshComponent),
                 lambda: (actor.get_components_by_class(unreal.StaticMeshComponent) or [None])[0]):
        try:
            got = call()
        except Exception:
            continue
        if got is not None:
            return got
    return None


def _note(tried, text):
    """per-actor 诊断只记一次：1500 个 Actor 不该把 tried 刷成 1500 行。"""
    if text not in tried:
        tried.append(text)


def _mat_path(obj):
    """材质对象到对象路径；读不到返回 None，跨版本签名漂移时不崩。"""
    if obj is None:
        return None
    try:
        return str(obj.get_path_name())
    except Exception:
        return None


def _mat_pkg(path):
    """对象路径到包裹路径：/Engine/Foo/Bar.Bar 去掉导出名；None 原样返回。"""
    if not path:
        return None
    return path.split(".")[0]


def _mat_is_engine_default(path):
    return _mat_pkg(path) in _ENGINE_DEFAULT_MATERIALS


def _slot_deviates(candidate, base):
    """单个槽是否偏离网格自身材质，返回 (deviates, decided)。

    decided=False 表示判不准（基线或候选读不到），上层走 unknown 通道放行加记账，
    不得当覆盖拒收。判等从宽到严三档：整路径相同；同包裹不同导出名（同名导出的
    变体对象）；两侧都是引擎隐式默认材质。第三档是 5.4 引擎 Cube 的实况：网格槽
    声明为 None 时，编辑器往覆盖数组预填的是 WorldGridMaterial，而网格侧给的是
    BasicShapeMaterial，同一个默认外观两条路径，不认这条就会把整组误判成有覆盖。
    """
    cp = _mat_path(candidate)
    if cp is None:
        return False, False
    bp = _mat_path(base)
    if bp is None:
        if _mat_is_engine_default(cp):
            return False, True
        return False, False
    if cp == bp or _mat_pkg(cp) == _mat_pkg(bp):
        return False, True
    if _mat_is_engine_default(cp) and _mat_is_engine_default(bp):
        return False, True
    return True, True


def _override_count(unreal, actor, tried):
    """材质覆盖数：只统计和网格自身材质不一样的槽。

    5.4 真机四连踩点，口径演进：
      1) 数 get_materials() 长度 = 解析后全部槽材质 -> 无覆盖也报有覆盖；
      2) 组件解析值比网格 static_materials[i].material_interface 槽声明
         -> 引擎 BasicShapes 槽声明为 None、解析时代入默认材质，误判；
      3) 直接数 override_materials 里的非空项 -> 编辑器把网格自身材质预填进
         覆盖数组（1500 个散装 Cube 全被报有覆盖），还是误判；
      4) 候选与 mesh.get_material(i) 逐路径比 -> 引擎 Cube 预填 WorldGridMaterial，
         网格侧返回 BasicShapeMaterial，同一默认外观两条路径，仍然误判。
    正解：同口径比路径，判等规则见 _slot_deviates；判不准走 unknown 通道；真偏离
    时留最多 3 条 mismatch 证据，下次误判能直接看出是哪两条路径在打架。
    """
    comp = _mesh_component(unreal, actor)
    if comp is None:
        return None
    mesh = _u.prop(comp, "static_mesh")
    if mesh is None:
        return None
    direct, _key, _sub = _u.get_prop_any(comp, ("override_materials",))
    total = None
    for call in (lambda: len(list(direct)) if direct is not None else None,
                 lambda: comp.get_num_materials(),
                 lambda: len(comp.get_materials()),
                 lambda: int(mesh.get_num_sections(0))):
        try:
            value = call()
        except Exception:
            continue
        if value:
            total = int(value)
            break
    if not total:
        _note(tried, "override_probe:slot_count_unreadable")
        return None

    if direct is not None:
        _note(tried, "override_source:override_materials")
    else:
        _note(tried, "override_source:comp.get_material")

    slot_defaults = None
    defaults, _api, _sub2, unreadable = _u.mesh_materials(unreal, mesh)
    slot_defaults = defaults if not unreadable else None

    def mesh_base(index):
        try:
            value = mesh.get_material(index)
        except Exception:
            value = None
        if value is not None:
            return value
        if slot_defaults is None:
            return None
        try:
            return slot_defaults[index]
        except Exception:
            return None

    hits = 0
    unknown = 0
    evidence = []
    for index in range(total):
        candidate = None
        if direct is not None:
            try:
                candidate = list(direct)[index]
            except Exception:
                candidate = None
        if candidate is None:
            try:
                candidate = comp.get_material(index)
            except Exception:
                _note(tried, "override_probe:comp.get_material_unavailable")
                return None
        if candidate is None:
            continue
        base = mesh_base(index)
        deviates, decided = _slot_deviates(candidate, base)
        if not decided:
            unknown += 1
            continue
        if deviates:
            hits += 1
            if len(evidence) < 3:
                evidence.append("%s!=%s" % (_mat_path(candidate), _mat_path(base)))
    if evidence:
        _note(tried, "override_mismatch:%d:%s" % (hits, ";".join(evidence)))
    if hits == 0 and unknown:
        _note(tried, "override_probe:%d/%d_baseline_unreadable" % (unknown, total))
        return None
    return hits

def _actor_record(unreal, actor, tried):
    """保真采集：回滚需要类/网格/变换/标签/子关卡，其余（碰撞、光照绑定）不承诺。"""
    cls = ""
    try:
        cls = actor.get_class().get_name()
    except Exception:
        pass
    rec = {"name": "", "class": cls, "label": None, "level": None,
           "location": list(_ORIGIN), "rotation": list(_ORIGIN),
           "scale": [1.0, 1.0, 1.0], "mesh": None, "material_overrides": 0,
           "tags": [], "hidden": False}
    try:
        rec["name"] = str(actor.get_name())
    except Exception:
        pass
    for key, getter in (("location", lambda: actor.get_actor_location()),
                        ("rotation", lambda: actor.get_actor_rotation()),
                        ("scale", lambda: actor.get_actor_scale3d())):
        try:
            rec[key] = _to_list(getter())
        except Exception:
            pass
    try:
        rec["label"] = str(actor.get_actor_label())
    except Exception:
        pass
    rec["level"] = _ac._level_name(actor)
    mesh, _inst = _ac._mesh_probe(unreal, actor, cls)
    rec["mesh"] = mesh
    if cls == STATIC_MESH_ACTOR:
        rec["material_overrides"] = _override_count(unreal, actor, tried)
    try:
        rec["tags"] = [str(t) for t in actor.tags]
    except Exception:
        pass
    try:
        rec["hidden"] = bool(actor.is_hidden_game())
    except Exception:
        pass
    return rec


def _rotator(unreal, values, tried):
    for maker in (lambda: unreal.Rotator(float(values[0]), float(values[1]),
                                         float(values[2])),
                  lambda: unreal.Rotator(pitch=float(values[0]), yaw=float(values[1]),
                                         roll=float(values[2]))):
        try:
            return maker()
        except Exception:
            continue
    tried.append("Rotator:ctor_miss")
    return None


def _load_mesh(unreal, mesh_path, tried):
    obj = _u.load_object(unreal, mesh_path)
    if obj is None:
        tried.append("load_asset_miss:%s" % mesh_path)
    return obj


def _spawn_actor(unreal, world, class_name, location, rotation, tried, evidence):
    cls = getattr(unreal, str(class_name), None)
    if cls is None:
        tried.append("class:%s:missing" % class_name)
        return None
    attempts = []
    if world is not None:
        attempts.append(("EditorLevelLibrary.spawn_actor_from_class",
                         lambda: unreal.EditorLevelLibrary.spawn_actor_from_class(
                             cls, location, rotation)))
        attempts.append(("GameBaseLibrary.spawn_actor_from_class",
                         lambda: unreal.GameBaseLibrary.spawn_actor_from_class(
                             world, cls, location, rotation)))
    for sub_name, method in (("EditorActorSubsystem", "spawn_actor_from_class"),
                             ("LevelEditorSubsystem", "new_actor_from_class")):
        sub = None
        try:
            sub = unreal.get_editor_subsystem(getattr(unreal, sub_name))
        except Exception:
            sub = None
        if sub is not None:
            attempts.append(("%s.%s" % (sub_name, method),
                             lambda sub=sub, method=method: getattr(sub, method)(
                                 cls, location, rotation)))
    for label, call in attempts:
        try:
            actor = call()
        except Exception as exc:
            tried.append("%s:%s" % (label, type(exc).__name__))
            continue
        if actor is not None:
            tried.append(label)
            if evidence is not None:
                evidence.append(label)
            return actor
    return None


def _hism_comp_class(unreal):
    """HISM 组件类名：5.4 实证是 HierarchicalInstancedStaticMeshComponent。

    HierarchicalStaticMeshComponent 这个"想当然"的写法在 5.4 根本不存在
    （dir(unreal) 里没有，UnrealEditor-Engine.dll 的名字表里也没有），
    照它找组件会永远 miss；这里按真机名优先、旧猜测名兜底。
    """
    for name in _HISM_COMP_CLASS_NAMES:
        cls = getattr(unreal, str(name), None)
        if cls is not None:
            return cls, str(name)
    return None, None


def _hism_component(unreal, actor, tried):
    cls, _hit = _hism_comp_class(unreal)
    probes = []
    if cls is not None:
        probes.append(("by_class", lambda: actor.get_component_by_class(cls)))
    ism = getattr(unreal, "InstancedStaticMeshComponent", None)
    if ism is not None:
        probes.append(("by_ism_base", lambda: actor.get_component_by_class(ism)))
    probes.append(("by_named_prop",
                   lambda: getattr(actor, "instanced_static_mesh_component", None)))
    probes.append(("first_component",
                   lambda: (actor.get_components_by_class(unreal.ActorComponent) or [None])[0]))
    for label, call in probes:
        try:
            comp = call()
        except Exception:
            continue
        if comp is not None:
            _note(tried, "hism_component:%s" % label)
            return comp
    tried.append("hism_component:miss")
    return None


def _attach_hism_component(unreal, actor, tried):
    """给裸 Actor 挂一个 HISM 根组件（5.4 真机实测口径）。

    5.4 的 Python 侧没有 Actor.add_component_by_class（C++ 那版是
    ScriptNoExport/BlueprintInternalUseOnly，不进脚本绑定），unreal.new_object
    才是可行入口：以 Actor 为 outer 造组件，再把 root_component 指过去。
    存盘重载后组件与实例都还在（probe_persist 实测 3 个实例存活），
    所以这条路不是"内存里糊弄一下"，是真的能落地到 .umap。
    """
    cls, hit = _hism_comp_class(unreal)
    if cls is None:
        _note(tried, "hism_comp_class:missing")
        return None
    comp = None
    for label, call in (("named", lambda: unreal.new_object(
        cls, actor, _HISM_COMP_NAME)),
        ("anon", lambda: unreal.new_object(cls, actor))):
        try:
            comp = call()
        except Exception as exc:
            _note(tried, "new_object_hism:%s(%s)" % (label, type(exc).__name__))
            continue
        if comp is not None:
            _note(tried, "new_object_hism:%s(%s)" % (label, hit))
            break
    if comp is None:
        return None
    ok, prop_hit, _sub = _u.set_prop_any(actor, ("root_component",), comp)
    if ok:
        _note(tried, "root_component:%s" % prop_hit)
    else:
        try:
            actor.set_root_component(comp)
            _note(tried, "set_root_component")
        except Exception as exc:
            _note(tried, "root_component:miss(%s)" % type(exc).__name__)
    return comp


def _make_hism_actor(unreal, world, tried):
    """造折叠容器：优先专用 Actor 类，没有就裸 Actor + HISM 根组件。

    返回 (actor, comp, path)；path 进清单与 tried，让"这版引擎走的哪条路"可追。
    """
    for cls_name in _HISM_ACTOR_CLASS_NAMES:
        actor = _spawn_actor(unreal, world, cls_name, unreal.Vector(*_ORIGIN),
                             _rotator(unreal, _ORIGIN, tried), tried, None)
        if actor is None:
            continue
        comp = _hism_component(unreal, actor, tried)
        if comp is not None:
            return actor, comp, "actor_class:%s" % cls_name
        _destroy(unreal, world, actor, tried)
        _note(tried, "actor_class:%s:no_component" % cls_name)
    actor = _spawn_actor(unreal, world, "Actor", unreal.Vector(*_ORIGIN),
                         _rotator(unreal, _ORIGIN, tried), tried, None)
    if actor is None:
        return None, None, "plain_actor:spawn_failed"
    comp = _attach_hism_component(unreal, actor, tried)
    if comp is None:
        _destroy(unreal, world, actor, tried)
        return None, None, "plain_actor:attach_failed"
    return actor, comp, "plain_actor+new_object"


def _copy_component_fidelity(unreal, source, comp, tried):
    """把源组件的碰撞/阴影/可移动性抄给新组件（读不到就跳过，不假装一致）。

    折叠承诺的是网格与变换；这三项决定"看着一样但点不动/没影子"，
    所以尽力保真，并把没保住的项如实记进 tried。
    """
    if source is None:
        _note(tried, "fidelity:no_source_component")
        return []
    missing = []
    for prop in ("mobility", "b_cast_shadow", "collision_profile_name"):
        try:
            value = source.get_editor_property(prop)
        except Exception:
            missing.append(prop)
            continue
        if value is None:
            missing.append(prop)
            continue
        try:
            comp.set_editor_property(prop, value)
        except Exception:
            missing.append(prop)
            continue
        _note(tried, "fidelity:%s" % prop)
    if missing:
        _note(tried, "fidelity_skipped:%s" % ",".join(missing))
    return missing


def _set_component_mesh(comp, mesh_obj, tried):
    ok, hit, _sub = _u.set_prop_any(comp, ("static_mesh",), mesh_obj)
    if ok:
        tried.append("component_static_mesh:%s" % hit)
        return True
    try:
        comp.set_static_mesh(mesh_obj)
        if _u.prop(comp, "static_mesh") is not None:
            tried.append("set_static_mesh")
            return True
    except Exception as exc:
        tried.append("set_static_mesh:%s" % type(exc).__name__)
    tried.append("component_mesh:miss")
    return False


def _add_instances(comp, transforms, tried):
    """批量写实例：5.4 实测口径，计数不信返回值。

    真机针对本函数的两个坑：单参数 add_instances(list) 直接 TypeError
    （绑定的是 (transforms, bWorldSpace) 双参）；双参能成但返回的是空列表，
    所以写入数只能取前后 get_instance_count() 的差。源变换取自
    actor.get_actor_transform()（世界空间），故 world-space 优先；引擎不接再退
    local-space（容器落在原点，两者等价），最后才逐实例兜底。
    """
    if not transforms:
        return 0
    before = _instance_count(comp)
    for label, world_space in (("world", True), ("local", False)):
        try:
            comp.add_instances(transforms, world_space)
        except Exception as exc:
            _note(tried, "add_instances_%s:%s" % (label, type(exc).__name__))
            continue
        _note(tried, "add_instances:%s" % label)
        return _added_count(comp, before, len(transforms))
    try:
        comp.add_instances(transforms)
    except Exception as exc:
        _note(tried, "add_instances_one_arg:%s" % type(exc).__name__)
    else:
        _note(tried, "add_instances:one_arg")
        return _added_count(comp, before, len(transforms))
    done = 0
    for transform in transforms:
        for call in (lambda: comp.add_instance(transform, True),
                     lambda: comp.add_instance(transform, False),
                     lambda: comp.add_instance(transform)):
            try:
                call()
                done += 1
                break
            except Exception as exc:
                _note(tried, "add_instance:%s" % type(exc).__name__)
    if done:
        _note(tried, "add_instance(loop)")
    return done


def _added_count(comp, before, wanted):
    """实例数以 get_instance_count() 为准；读不到才退回 wanted。"""
    after = _instance_count(comp)
    if after is None:
        return wanted
    if before is None:
        return after
    return max(0, after - before)


def _instance_count(comp):
    try:
        value = comp.get_instance_count()
        if value is not None:
            return int(value)
    except Exception:
        pass
    instances = _u.prop(comp, "instances")
    try:
        return len(instances)
    except Exception:
        return None


def _destroy(unreal, world, actor, tried):
    for label, call in (("Actor.destroy_actor", lambda: actor.destroy_actor()),
                        ("Actor.destroy", lambda: actor.destroy()),
                        ("EditorActorSubsystem.destroy_actor",
                         lambda: unreal.get_editor_subsystem(
                             unreal.EditorActorSubsystem).destroy_actor(actor)),
                        ("EditorLevelLibrary.destroy_actor",
                         lambda: unreal.EditorLevelLibrary.destroy_actor(actor)),
                        ("GameBaseLibrary.destroy_actor",
                         lambda: unreal.GameBaseLibrary.destroy_actor(world, actor))):
        try:
            value = call()
        except Exception as exc:
            tried.append("%s:%s" % (label, type(exc).__name__))
            continue
        if value is not False:
            tried.append(label)
            return True
    return False


def _actor_by_name(actors_by_name, name):
    return actors_by_name.get(str(name))


def _refresh_names(unreal):
    out = {}
    for actor in _all_actors(unreal, []):
        try:
            out[str(actor.get_name())] = actor
        except Exception:
            continue
    return out


def _save_level(unreal, tried):
    for label, call in (("EditorLoadingAndSavingUtils.save_dirty_map_packages",
                         lambda: unreal.EditorLoadingAndSavingUtils.save_dirty_map_packages(
                             [], True)),
                        ("EditorLoadingAndSavingUtils.save_dirty_packages",
                         lambda: unreal.EditorLoadingAndSavingUtils.save_dirty_packages(
                             [], True)),
                        ("EditorLevelLibrary.save_current_level",
                         lambda: unreal.EditorLevelLibrary.save_current_level())):
        try:
            call()
        except Exception as exc:
            tried.append("%s:%s" % (label, type(exc).__name__))
            continue
        tried.append(label)
        return True
    return False


@register
def opt_collapse_hism(mode="plan", mesh=None, min_count=5, limit=20, max_actors=20000,
                      class_contains=None, tag=None, manifest=None, allow_blockers=False,
                      allow_material_overrides=False, dry_run=True, confirm=False):
    """把同网格散装 Actor 折叠成 HISM（写·双钥；自带回滚清单）。

    mode=plan（默认，只出方案与拒收原因）/ collapse（建 HISM、迁实例、销毁源，
    并写 JSON 回滚清单）/ restore（按清单重建源 Actor、销毁 HISM）。
    UE 没有关卡 Actor 级事务回滚：collapse 前先提交 VCS 或另存关卡副本，本函数
    只提供自建清单级回滚，不承诺恢复碰撞/光照/蓝图绑定等细节。无引擎时结构键
    常驻、note=no_engine，绝不假执行。
    """
    mode = str(mode or "plan").strip().lower()
    if mode not in ("plan", "collapse", "restore"):
        return envelope.make_err(envelope.Code.RUNTIME_ERROR,
                                 "%s mode 需 plan|collapse|restore，got %r" % (OP, mode))
    if mode in ("collapse", "restore"):
        # 意图先于一切：没带双钥的写请求，连引擎都不该碰
        guard = _u.double_key_guard(OP + ("" if mode == "collapse" else "(restore)"),
                                    dry_run, confirm)
        if guard is not None:
            return guard
    if mode == "restore" and not manifest:
        return envelope.make_err(
            envelope.Code.RUNTIME_ERROR,
            "%s mode=restore 需 manifest=<折叠时写出的清单路径>" % OP)
    result = _base(mode, dry_run)
    result["filters"] = {"mesh": mesh, "min_count": _int(min_count, 5),
                         "limit": _int(limit, 20),
                         "max_actors": _int(max_actors, 20000),
                         "class_contains": class_contains, "tag": tag,
                         "manifest": manifest,
                         "allow_blockers": _u.to_bool(allow_blockers, False),
                         "allow_material_overrides": _u.to_bool(
                             allow_material_overrides, False)}
    try:
        import unreal
    except Exception:
        result["note"] = "no_engine"
        return envelope.make_ok(result)

    tried = result["tried"]
    world = _editor_world(unreal, tried)
    all_actors = _all_actors(unreal, tried)
    if all_actors is None:
        result["note"] = "取不到关卡 Actor（签名漂移？或编辑器里没打开关卡）"
        return envelope.make_ok(result)
    result["universe"] = len(all_actors)

    if mode == "restore":
        return _restore(unreal, world, result, tried, confirm)

    rows = []
    live = {}
    buckets = {}
    scanned = 0
    for actor in list(all_actors)[:_int(max_actors, 20000)]:
        rec = _actor_record(unreal, actor, tried)
        if class_contains and str(class_contains).lower() not in str(rec["class"]).lower():
            continue
        if tag and not _ac._has_tag(actor, tag):
            continue
        scanned += 1
        if not rec["mesh"]:
            continue
        buckets.setdefault(rec["mesh"], []).append(rec)
        live[rec["name"]] = actor
        overrides = rec["material_overrides"]
        if overrides is None:
            result["material_override_unknown"] += 1
        rows.append((rec["mesh"], rec["class"], rec["level"], rec["name"],
                     bool(overrides)))
    selection = _select_groups(rows, mesh=mesh, min_count=_int(min_count, 5),
                               limit=_int(limit, 20), allow_blockers=allow_blockers,
                               allow_material_overrides=allow_material_overrides)
    result["scanned"] = scanned
    result["total"] = selection["collapsible_groups"]
    result["cap"] = _int(limit, 20)
    result["truncated"] = selection["collapsible_groups"] > len(selection["groups"])
    result["blockers_refused"] = selection["blockers_refused"]
    result["material_override_refused"] = selection["material_override_refused"]
    result["drawcalls_before"] = selection["drawcalls_before"]
    result["drawcalls_after"] = selection["drawcalls_after"]
    result["planned"] = selection["collapsible_actors"]
    result["rejected"] = len(selection["refused"])
    result["refused_groups"] = [{"mesh": g["mesh"], "reason": g["refused"],
                                 "actors": g["actors"],
                                 "material_overrides": g["material_overrides"]}
                                for g in selection["refused"][:20]]
    result["groups"] = [{"mesh": g["mesh"], "actors": g["actors"], "levels": g["levels"],
                         "actor_classes": g["actor_classes"], "blockers": g["blockers"],
                         "material_overrides": g["material_overrides"],
                         "hism_actor": None, "instanced": 0, "destroyed": 0}
                        for g in selection["groups"]]
    if not selection["groups"]:
        result["note"] = ("无可折叠组（没有 >= %d 重复的同网格，或组被 blockers/材质覆盖"
                          "拒收，或关卡没打开）" % _int(min_count, 5))
        return envelope.make_ok(result)

    if _u.to_bool(dry_run, True):
        result["note"] = ("dry_run：可折叠 %d 个 Actor 到 %d 个 HISM（drawcall 估 %d -> %d），"
                          "未改场景。落盘需 dry_run=False 且 confirm=True。"
                          % (selection["collapsible_actors"], len(selection["groups"]),
                             selection["drawcalls_before"],
                             selection["drawcalls_after"]))
        return envelope.make_ok(result)
    return _collapse(unreal, world, live, buckets, selection, result, tried, manifest)


def _collapse(unreal, world, live, buckets, selection, result, tried, manifest):
    payload = {"version": MANIFEST_VERSION, "op": OP, "ue_version": _ue_version(unreal),
               "project": _u_project_dir(unreal),
               "created": time.strftime("%Y-%m-%dT%H:%M:%S"), "groups": []}
    expected_sources = 0
    for index, group in enumerate(selection["groups"]):
        mesh_path = group["mesh"]
        recs = buckets.get(mesh_path) or []
        row = result["groups"][index]
        transforms = []
        unreadable = 0
        for rec in recs:
            actor = live.get(rec["name"])
            if actor is None:
                _fail(result, {"mesh": mesh_path, "actor": rec["name"],
                          "note": "actor_lost"})
                unreadable += 1
                continue
            try:
                transforms.append(actor.get_actor_transform())
            except Exception as exc:
                _note(tried, "get_actor_transform:%s" % type(exc).__name__)
                unreadable += 1
        # 有一个源的世界变换读不到就整组不动：宁可保持原样，也不能
        # "折叠成功"后发现少了几个实例——那几个 Actor 已经被销毁了。
        if unreadable:
            row["note"] = "transform_unreadable:%d" % unreadable
            result["failed"].append({"mesh": mesh_path, "note": "transform_unreadable",
                                     "actors": unreadable})
            continue
        actor, comp, spawn_path = _make_hism_actor(unreal, world, tried)
        if actor is None or comp is None:
            row["note"] = "spawn_hism_failed"
            result["failed"].append({"mesh": mesh_path, "note": "spawn_hism_failed",
                                     "path": spawn_path})
            continue
        mesh_obj = _load_mesh(unreal, mesh_path, tried)
        if mesh_obj is None or not _set_component_mesh(comp, mesh_obj, tried):
            _destroy(unreal, world, actor, tried)
            result["failed"].append({"mesh": mesh_path, "note": "set_mesh_failed"})
            continue
        _copy_component_fidelity(unreal, _mesh_component(unreal, live.get(recs[0]["name"]))
                                 if recs else None, comp, tried)
        added = _add_instances(comp, transforms, tried)
        # 实例没写满就销毁源 Actor 等于丢几何，必须把容器收回去、整组退回
        if added < len(transforms):
            _destroy(unreal, world, actor, tried)
            row["note"] = "instance_write_short"
            result["failed"].append({"mesh": mesh_path, "note": "instance_write_short",
                                     "added": added, "wanted": len(transforms)})
            continue
        count = _instance_count(comp)
        if count is not None and count < added:
            tried.append("instance_count_short:%s<%s" % (count, added))
        gone = 0
        for rec in recs:
            source = live.get(rec["name"])
            if source is None:
                gone += 1
                continue
            if _destroy(unreal, world, source, tried):
                gone += 1
        expected_sources += len(recs)
        result["hism_created"] += 1
        result["changed"] += 1
        result["actors_instanced"] += added
        result["actors_destroyed"] += gone
        hism_name = ""
        try:
            hism_name = str(actor.get_name())
        except Exception:
            pass
        try:
            actor.set_actor_label("%s_HISM" % _mesh_stem(mesh_path))
        except Exception:
            pass
        for dirty in (lambda: actor.modify(), lambda: comp.modify(),
                      lambda: actor.mark_actor_dirty()):
            try:
                dirty()
            except Exception:
                continue
        row["hism_actor"] = hism_name
        row["spawn_path"] = spawn_path
        row["instanced"] = added
        row["destroyed"] = gone
        payload["groups"].append({"mesh": mesh_path, "hism_actor": hism_name,
                                  "hism_class": spawn_path, "instances": added,
                                  "sources": recs})
    result["executed"] = True
    result["groups_planned"] = len(selection["groups"])
    if not payload["groups"]:
        result["note"] = "一个组都没折叠成功（见 failed/tried）"
        result["success"] = False
        return envelope.make_ok(result)
    path = _manifest_path(unreal, manifest)
    try:
        _write_manifest(path, payload)
        result["manifest_path"] = path
        result["rollback_available"] = True
    except Exception as exc:
        result["failed"].append({"note": "manifest_write_failed",
                                 "err": type(exc).__name__})
    saved = _save_level(unreal, tried)
    result["level_saved"] = bool(saved)
    result["success"] = (result["hism_created"] == len(selection["groups"])
                         and result["actors_destroyed"] >= expected_sources
                         and bool(result["rollback_available"]))
    result["note"] = ("已折叠 %d 组 / 销毁 %d Actor -> %d 个 HISM（实例 %d，drawcall 估 %d -> %d）。"
                      "回滚清单 %s；关卡%s。undo 栈不可用，核对后请提交 VCS。"
                      % (result["hism_created"], result["actors_destroyed"],
                         result["hism_created"], result["actors_instanced"],
                         result["drawcalls_before"], result["hism_created"],
                         result["manifest_path"] or "写入失败",
                         "已保存" if saved else "未保存（需手动 Save）"))
    return envelope.make_ok(result)


def _restore(unreal, world, result, tried, confirm):
    path = result["filters"].get("manifest")
    if not path:
        result["note"] = "mode=restore \u9700 manifest=<\u6298\u53e0\u65f6\u5199\u7684\u6e05\u5355\u8def\u5f84>"
        return envelope.make_ok(result)
    payload = _read_manifest(path)
    if payload is None:
        result["note"] = "\u6e05\u5355\u8bfb\u4e0d\u5230\u6216\u7ed3\u6784\u4e0d\u7b26\uff1a%s" % path
        return envelope.make_ok(result)
    result["manifest_path"] = str(path)
    sources = []
    for group in payload["groups"]:
        for rec in group.get("sources") or []:
            sources.append((group, rec))
    result["planned"] = len(sources)
    result["total"] = len(payload["groups"])
    result["groups"] = [{"mesh": g.get("mesh"), "hism_actor": g.get("hism_actor"),
                         "actors": len(g.get("sources") or [])}
                        for g in payload["groups"]]
    if _u.to_bool(result["dry_run"], True):
        result["note"] = ("dry_run\uff1a\u53ef\u6309\u6e05\u5355\u91cd\u5efa %d \u4e2a\u6e90 Actor\u3001\u9500\u6bc1 %d \u4e2a HISM\u3002"
                          "\u843d\u76d8\u9700 dry_run=False \u4e14 confirm=True\u3002"
                          % (len(sources), len(payload["groups"])))
        return envelope.make_ok(result)
    names = _refresh_names(unreal)
    recreated = 0
    for group, rec in sources:
        location = unreal.Vector(*[float(v) for v in (rec.get("location") or _ORIGIN)])
        rotation = _rotator(unreal, rec.get("rotation") or _ORIGIN, tried)
        if rotation is None:
            _fail(result, {"actor": rec.get("name"), "note": "rotator_failed"})
            continue
        class_name = rec.get("class") or STATIC_MESH_ACTOR
        actor = _spawn_actor(unreal, world, class_name, location, rotation, tried, None)
        if actor is None:
            _fail(result, {"actor": rec.get("name"), "note": "spawn_failed",
                           "class": class_name})
            continue
        result["recreated"] = result.get("recreated", 0) + 1
        recreated += 1
        for call in (lambda: actor.set_actor_scale3d(
            unreal.Vector(*[float(v) for v in (rec.get("scale") or [1, 1, 1])])),
            lambda: actor.set_actor_scale(
                unreal.Vector(*[float(v) for v in (rec.get("scale") or [1, 1, 1])]))):
            try:
                call()
                break
            except Exception:
                continue
        mesh_path = rec.get("mesh")
        if mesh_path:
            # \u7f51\u683c\u53ea\u8ba4\u56de\u8bfb\uff0c\u4e0d\u4fe1\u8c03\u7528\u6ca1\u62a5\u9519\uff1aAActor \u65e2\u6ca1\u6709 static_mesh \u5c5e\u6027\uff0c
            # \u4e5f\u6ca1\u6709 set_static_mesh \u65b9\u6cd5\uff085.4 \u771f\u673a\u8e29\u8fc7\u2014\u2014\u56de\u6eda\u62a5 success\uff0c\u5374\u7559\u4e0b 1500
            # \u4e2a\u7a7a Actor\uff09\u3002\u6b63\u786e\u5bf9\u8c61\u662f\u5b83\u7684 StaticMeshComponent\uff0c\u5199\u5b8c\u7528 _mesh_probe \u56de\u8bfb\u6bd4\u5bf9\u3002
            _write_back_mesh(unreal, actor, str(mesh_path), result, tried)
        if rec.get("label"):
            for call in (lambda: actor.set_actor_label(str(rec["label"])),
                         lambda: _u.set_prop_any(actor, ("actor_label",), str(rec["label"]))):
                try:
                    call()
                    break
                except Exception:
                    continue
    destroyed = 0
    for group in payload["groups"]:
        hism_name = group.get("hism_actor")
        if not hism_name:
            continue
        victim = _actor_by_name(names, hism_name)
        if victim is None:
            result["skipped"].append({"note": "hism_not_found", "actor": hism_name})
            continue
        if _destroy(unreal, world, victim, tried):
            destroyed += 1
    result["executed"] = True
    result["changed"] = recreated
    result["actors_destroyed"] = destroyed
    result["success"] = (recreated > 0 and not result["failed"]
                         and result["restore_mesh_missing"] == 0)
    saved = _save_level(unreal, tried)
    result["level_saved"] = bool(saved)
    note = ("\u5df2\u91cd\u5efa %d \u4e2a\u6e90 Actor\uff08\u6e05\u5355\u8bb0\u5f55 %d\uff09\uff0c\u9500\u6bc1 HISM %d \u4e2a\uff1b\u5173\u5361%s\u3002"
            % (recreated, len(sources), destroyed,
               "\u5df2\u4fdd\u5b58" if saved else "\u672a\u4fdd\u5b58"))
    if result["restore_mesh_missing"]:
        note += ("\u8b66\u544a\uff1a%d \u4e2a\u91cd\u5efa\u51fa\u6765\u7684 Actor \u7f51\u683c\u6ca1\u5199\u8fdb\u53bb\uff08\u5f15\u64ce\u6ca1\u7ed9\u5230\u53ef\u5199\u7684 "
                 "StaticMeshComponent \u63a5\u53e3\uff09\uff0c\u5b83\u4eec\u73b0\u5728\u662f\u7a7a\u58f3\uff0csuccess \u5df2\u7f6e False\uff1b"
                 "\u91cd\u5efa\u5173\u5361\u526f\u672c\u6216\u624b\u5de5\u8865\u7f51\u683c\u540e\u518d\u7528\u3002"
                 % result["restore_mesh_missing"])
    note += "\u7ec6\u8282\u5c5e\u6027\uff08\u78b0\u649e/\u5149\u7167\u7ed1\u5b9a\uff09\u4e0d\u627f\u8bfa\u5b8c\u5168\u590d\u539f\u3002"
    result["note"] = note
    return envelope.make_ok(result)


def _write_back_mesh(unreal, actor, mesh_path, result, tried):
    """\u628a\u6e05\u5355\u91cc\u7684\u7f51\u683c\u5199\u56de\u7ec4\u4ef6\u5e76\u56de\u8bfb\u6821\u9a8c\uff1b\u843d\u4e0d\u5230\u4f4d\u7f6e\u8bb0\u4e00\u7b14\u3002"""
    actor_cls = ""
    try:
        actor_cls = str(actor.get_class().get_name())
    except Exception:
        pass

    def landed():
        return _ac._mesh_probe(unreal, actor, actor_cls)[0] == mesh_path

    if landed():
        return
    mesh_obj = _load_mesh(unreal, mesh_path, tried)
    if mesh_obj is not None:
        sub = []
        attempts = []
        comp = _mesh_component(unreal, actor)
        if comp is not None:
            attempts.append(lambda comp=comp: _set_component_mesh(comp, mesh_obj, sub))
        attempts.append(lambda: actor.set_static_mesh(mesh_obj))
        attempts.append(lambda: _u.set_prop_any(actor, ("static_mesh",), mesh_obj))
        for attempt in attempts:
            try:
                attempt()
            except Exception as exc:
                _note(tried, "restore_set_mesh:%s" % type(exc).__name__)
            for item in sub:
                _note(tried, item)
            if landed():
                break
    if not landed():
        result["restore_mesh_missing"] += 1
        _fail(result, {"mesh": mesh_path, "note": "mesh_not_applied"})