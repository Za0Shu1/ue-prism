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
HISM_CLASS = "HierarchicalStaticMeshActor"
STATIC_MESH_ACTOR = _oi.STATIC_MESH_ACTOR
MANIFEST_VERSION = 1
_ORIGIN = (0.0, 0.0, 0.0)


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
            "manifest_path": None, "rollback_available": False}


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


def _override_count(unreal, actor, tried):
    """材质覆盖数：只认"这个 Actor 自己改过的槽"，不认引擎代入的默认材质。

    5.4 真实工程两次踩点：
      1) 数 `get_materials()` 长度 = 解析后的全部槽材质（含默认值）-> 无覆盖也报有覆盖；
      2) 拿 `get_material(i)` 与网格 `static_materials[i].material_interface`（槽声明，
         引擎 Cube 上是 None/DefaultMaterial）比对 -> 引擎把默认材质代入解析，1500 个
         散装 Cube 全被误判成有覆盖，折叠闸门恒拒。
    所以优先级：override_materials（字面意义上的覆盖数组）> 组件与网格双侧
    `get_material(i)` 同口径比对 > 判不准返回 None（上层按未知记账并放行）。
    """
    comp = _mesh_component(unreal, actor)
    if comp is None:
        return None
    direct, key, _sub = _u.get_prop_any(comp, ("override_materials",))
    if direct is not None:
        tried.append("override_materials:%s" % key)
        try:
            return sum(1 for m in list(direct) if m is not None)
        except Exception:
            return None
    mesh = _u.prop(comp, "static_mesh")
    if mesh is None:
        return None
    total = None
    for call in (lambda: comp.get_num_materials(),
                 lambda: len(comp.get_materials()),
                 lambda: int(mesh.get_num_sections(0))):
        try:
            value = int(call())
        except Exception:
            continue
        if value:
            total = value
            break
    if not total:
        tried.append("override_probe:slot_count_unreadable")
        return None

    def path_of(obj):
        if obj is None:
            return None
        try:
            return str(obj.get_path_name())
        except Exception:
            return None

    hits = 0
    unknown = 0
    for index in range(total):
        try:
            on_comp = comp.get_material(index)
        except Exception:
            tried.append("override_probe:comp.get_material_unavailable")
            return None
        try:
            on_mesh = mesh.get_material(index)
        except Exception:
            tried.append("override_probe:mesh.get_material_unavailable")
            on_mesh = None
        comp_path = path_of(on_comp)
        mesh_path = path_of(on_mesh)
        if on_comp is None:
            continue
        if mesh_path is None:
            unknown += 1
            continue
        if comp_path != mesh_path:
            hits += 1
    if hits == 0 and unknown:
        tried.append("override_probe:%d/%d_slots_unreadable" % (unknown, total))
        return None
    tried.append("override_probe:resolved_vs_mesh")
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


def _hism_component(unreal, actor, tried):
    probes = [
        lambda: actor.get_component_by_class(unreal.HierarchicalStaticMeshComponent),
        lambda: actor.get_component_by_class(unreal.InstancedStaticMeshComponent),
        lambda: getattr(actor, "instanced_static_mesh_component", None),
        lambda: actor.get_components_by_class(unreal.ActorComponent)[0],
    ]
    for call in probes:
        try:
            comp = call()
        except Exception:
            continue
        if comp is not None:
            return comp
    tried.append("hism_component:miss")
    return None


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
    for label, call in (("add_instances", lambda: comp.add_instances(transforms)),
                        ("add_instances2", lambda: comp.add_instances(transforms, False))):
        try:
            call()
        except Exception as exc:
            tried.append("%s:%s" % (label, type(exc).__name__))
            continue
        tried.append(label)
        return len(transforms)
    done = 0
    for transform in transforms:
        for call in (lambda: comp.add_instance(transform, True),
                     lambda: comp.add_instance(transform)):
            try:
                call()
                done += 1
                break
            except Exception as exc:
                tried.append("add_instance:%s" % type(exc).__name__)
    if done:
        tried.append("add_instance(loop)")
    return done


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
        for rec in recs:
            actor = live.get(rec["name"])
            if actor is None:
                result["failed"].append({"mesh": mesh_path, "actor": rec["name"],
                                         "note": "actor_lost"})
                continue
            try:
                transforms.append(actor.get_actor_transform())
            except Exception as exc:
                tried.append("get_actor_transform:%s" % type(exc).__name__)
        actor = _spawn_actor(unreal, world, HISM_CLASS, unreal.Vector(*_ORIGIN),
                             _rotator(unreal, _ORIGIN, tried), tried, None)
        if actor is None:
            row["note"] = "spawn_hism_failed"
            result["failed"].append({"mesh": mesh_path, "note": "spawn_hism_failed"})
            continue
        comp = _hism_component(unreal, actor, tried)
        if comp is None:
            _destroy(unreal, world, actor, tried)
            result["failed"].append({"mesh": mesh_path, "note": "hism_component_missing"})
            continue
        mesh_obj = _load_mesh(unreal, mesh_path, tried)
        if mesh_obj is None or not _set_component_mesh(comp, mesh_obj, tried):
            _destroy(unreal, world, actor, tried)
            result["failed"].append({"mesh": mesh_path, "note": "set_mesh_failed"})
            continue
        added = _add_instances(comp, transforms, tried)
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
            actor.mark_actor_dirty()
        except Exception:
            pass
        row["hism_actor"] = hism_name
        row["instanced"] = added
        row["destroyed"] = gone
        payload["groups"].append({"mesh": mesh_path, "hism_actor": hism_name,
                                  "hism_class": HISM_CLASS, "instances": added,
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
        result["note"] = "mode=restore 需 manifest=<折叠时写的清单路径>"
        return envelope.make_ok(result)
    payload = _read_manifest(path)
    if payload is None:
        result["note"] = "清单读不到或结构不符：%s" % path
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
        result["note"] = ("dry_run：可按清单重建 %d 个源 Actor、销毁 %d 个 HISM。"
                          "落盘需 dry_run=False 且 confirm=True。"
                          % (len(sources), len(payload["groups"])))
        return envelope.make_ok(result)
    names = _refresh_names(unreal)
    recreated = 0
    for group, rec in sources:
        location = unreal.Vector(*[float(v) for v in (rec.get("location") or _ORIGIN)])
        rotation = _rotator(unreal, rec.get("rotation") or _ORIGIN, tried)
        if rotation is None:
            result["failed"].append({"actor": rec.get("name"), "note": "rotator_failed"})
            continue
        actor = _spawn_actor(unreal, world, rec.get("class") or STATIC_MESH_ACTOR,
                             location, rotation, tried, None)
        if actor is None:
            result["failed"].append({"actor": rec.get("name"), "note": "spawn_failed"})
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
            mesh_obj = _load_mesh(unreal, mesh_path, tried)
            if mesh_obj is not None:
                for call in (lambda: actor.set_static_mesh(mesh_obj),
                             lambda: _u.set_prop_any(actor, ("static_mesh",), mesh_obj)):
                    try:
                        call()
                        break
                    except Exception:
                        continue
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
    result["success"] = recreated > 0 and len(result["failed"]) == 0
    saved = _save_level(unreal, tried)
    result["level_saved"] = bool(saved)
    result["note"] = ("已重建 %d 个源 Actor（清单记录 %d），销毁 HISM %d 个；关卡%s。"
                      "细节属性（碰撞/光照绑定）不承诺完全复原。"
                      % (recreated, len(sources), destroyed,
                         "已保存" if saved else "未保存"))
    return envelope.make_ok(result)
