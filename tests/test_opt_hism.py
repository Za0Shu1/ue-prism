"""HISM 折叠（路线 A）离线用例：方案纯逻辑、双钥守卫、无引擎降级、假引擎全流程。

真机签名（spawn/add_instances/destroy/save）由 L2 夹具的 CalibMap 1500 Cube 靶
覆盖（docs/TESTING.md）；这里守的是"计划正确 + 没引擎时不假执行 + 回滚清单能闭环"。
"""
from __future__ import annotations

import json
import sys
import types

import pytest

from prism import envelope
from prism.domain import opt_hism as oh

CUBE = "/Game/PrismCalib/Cube"
OTHER = "/Game/PrismCalib/Sphere"


# ---------------- 纯逻辑：分组与拒收 ----------------

def _rows(cube=7, other=3, mat_override_cube=0, extra_class=0):
    rows = []
    for i in range(cube - extra_class):
        rows.append((CUBE, "StaticMeshActor", "PersistentLevel", "Cube_%d" % i, False))
    for i in range(extra_class):
        rows.append((CUBE, "BP_Placer_C", "PersistentLevel", "Placer_%d" % i, False))
    for i in range(other):
        rows.append((OTHER, "StaticMeshActor", "PersistentLevel", "Sph_%d" % i, False))
    return rows


def _rows_with_overrides(count, override_names=()):
    return [(CUBE, "StaticMeshActor", "PersistentLevel", "Cube_%d" % i,
             "Cube_%d" % i in override_names) for i in range(count)]


def test_select_groups_keeps_only_repeat_meshes():
    sel = oh._select_groups(_rows(cube=7, other=3), min_count=5, limit=10)
    assert sel["collapsible_groups"] == 1
    assert sel["groups"][0]["mesh"] == CUBE
    assert sel["drawcalls_before"] == 7 and sel["drawcalls_after"] == 1
    assert sel["collapsible_actors"] == 6          # 7 个源 -> 1 个 HISM，净省 6


def test_select_groups_refuses_blueprint_placers():
    sel = oh._select_groups(_rows(cube=7, extra_class=2), min_count=5, limit=10)
    assert sel["groups"] == [] and sel["blockers_refused"] == 1
    assert sel["refused"][0]["refused"] == "blockers"
    allowed = oh._select_groups(_rows(cube=7, extra_class=2), min_count=5, limit=10,
                                allow_blockers=True)
    assert len(allowed["groups"]) == 1 and allowed["blockers_refused"] == 0


def test_select_groups_refuses_material_overrides():
    rows = _rows_with_overrides(6, override_names={"Cube_3"})
    refused = oh._select_groups(rows, min_count=5, limit=10)
    assert refused["groups"] == []
    assert refused["material_override_refused"] == 1
    forced = oh._select_groups(rows, min_count=5, limit=10,
                               allow_material_overrides=True)
    assert len(forced["groups"]) == 1


def test_select_limits_groups_and_marks_over_limit():
    rows = _rows(cube=6, other=6)
    sel = oh._select_groups(rows, min_count=5, limit=1)
    assert len(sel["groups"]) == 1 and sel["truncated" if "truncated" in sel else "collapsible_groups"] == 2
    assert sel["refused"][0]["refused"] == "over_limit"


def test_select_mesh_filter_is_substring_and_case_insensitive():
    sel = oh._select_groups(_rows(cube=6, other=6), mesh="sphere", min_count=5, limit=5)
    assert [g["mesh"] for g in sel["groups"]] == [OTHER]


# ---------------- 无引擎降级 + 双钥 ----------------

def test_plan_without_engine_is_honest_and_structured():
    env = oh.opt_collapse_hism()
    res = env["result"]
    assert env["ok"] is True
    assert res["note"] == "no_engine" and res["executed"] is False
    assert res["success"] is False and res["hism_created"] == 0
    for key in ("mode", "groups", "refused_groups", "planned", "drawcalls_before",
                "drawcalls_after", "manifest_path", "rollback_available", "tried",
                "universe", "total", "truncated", "cap", "level_saved"):
        assert key in res, key


def test_bad_mode_is_a_structured_error():
    env = oh.opt_collapse_hism(mode="yolo")
    assert env["ok"] is False and env["error"]["code"] == envelope.Code.RUNTIME_ERROR


def test_collapse_without_confirm_is_refused_at_domain_level():
    env = oh.opt_collapse_hism(mode="collapse", dry_run=False, confirm=False)
    assert env["ok"] is False and "双钥" in env["error"]["message"]
    env2 = oh.opt_collapse_hism(mode="collapse", dry_run="false", confirm="0")
    assert env2["ok"] is False, "总线把 bool 送成字符串时不得被执行"


def test_restore_without_manifest_is_a_structured_error():
    env = oh.opt_collapse_hism(mode="restore", dry_run=False, confirm=True)
    assert env["ok"] is False and "manifest" in env["error"]["message"]


# ---------------- 假引擎：折叠 -> 清单 -> 回滚 ----------------

class _Vec(object):
    def __init__(self, x=0.0, y=0.0, z=0.0):
        self.x, self.y, self.z = float(x), float(y), float(z)


class _Rot(object):
    def __init__(self, pitch=0.0, yaw=0.0, roll=0.0):
        self.pitch, self.yaw, self.roll = float(pitch), float(yaw), float(roll)


class _Tr(object):
    def __init__(self, loc, rot, scale):
        self.translation, self.rotation, self.scale3d = loc, rot, scale


class _Cls(object):
    def __init__(self, name):
        self._name = name

    def get_name(self):
        return self._name


class _Lvl(object):
    def get_name(self):
        return "PersistentLevel"


class _MeshObj(object):
    def __init__(self, package):
        self.package = package

    def get_path_name(self):
        return self.package + "." + self.package.rsplit("/", 1)[-1]


class StaticMeshComponent(object):
    def __init__(self, mesh, overrides=0):
        self.static_mesh = mesh
        self._overrides = overrides

    def get_class(self):
        return _Cls("StaticMeshComponent")

    def get_materials(self):
        return [None] * self._overrides


class HierarchicalStaticMeshComponent(object):
    def __init__(self):
        self.static_mesh = None
        self.instances = []
        self.added_via = None

    def get_class(self):
        return _Cls("HierarchicalStaticMeshComponent")

    def add_instances(self, transforms, flag=False):
        self.instances.extend(transforms)
        self.added_via = "add_instances"
        return len(self.instances)

    def get_instance_count(self):
        return len(self.instances)


class InstancedStaticMeshComponent(HierarchicalStaticMeshComponent):
    pass


class ActorComponent(object):
    pass


class HierarchicalStaticMeshActor(object):
    pass


class StaticMeshActor(object):
    pass


class _Actor(object):
    def __init__(self, world, name, class_name, comps=None, loc=(0.0, 0.0, 0.0)):
        self._world = world
        self._name = name
        self._class = class_name
        self._comps = comps or []
        self._loc = loc
        self.label = name
        self.tags = []
        self.alive = True
        self.scale = _Vec(1.0, 1.0, 1.0)

    def get_class(self):
        return _Cls(self._class)

    def get_name(self):
        return self._name

    def get_level(self):
        return _Lvl()

    def get_actor_location(self):
        return _Vec(*self._loc)

    def get_actor_rotation(self):
        return _Rot()

    def get_actor_scale3d(self):
        return _Vec(self.scale.x, self.scale.y, self.scale.z)

    def get_actor_label(self):
        return self.label

    def get_actor_transform(self):
        return _Tr(self.get_actor_location(), self.get_actor_rotation(),
                   self.get_actor_scale3d())

    def is_hidden_game(self):
        return False

    def get_material_override(self):
        comp = self._comps[0] if self._comps else None
        n = len(comp.get_materials()) if comp is not None and hasattr(
            comp, "get_materials") else 0
        if not n:
            raise AttributeError("no overrides")
        return [None] * n

    def get_components_by_class(self, cls):
        return [c for c in self._comps if type(c).__name__ == cls.__name__]

    def get_component_by_class(self, cls):
        for comp in self._comps:
            if type(comp).__name__ == cls.__name__:
                return comp
        raise AttributeError(cls.__name__)

    def set_static_mesh(self, mesh):
        for comp in self._comps:
            if hasattr(comp, "static_mesh"):
                comp.static_mesh = mesh
                return

    def set_actor_scale3d(self, vec):
        self.scale = vec

    def set_actor_label(self, label):
        self.label = label

    def mark_actor_dirty(self):
        pass

    def destroy(self):
        self.alive = False
        self._world.actors.remove(self)
        return True


class _World(object):
    def __init__(self):
        self.actors = []
        self.counter = 0
        self.spawned_classes = []

    def spawn(self, cls, loc=None, rot=None):
        name = "%s_%d" % (cls.__name__, len(self.actors))
        self.counter += 1
        comps = []
        if cls.__name__ == "HierarchicalStaticMeshActor":
            comps = [HierarchicalStaticMeshComponent()]
        elif cls.__name__ == "StaticMeshActor":
            comps = [StaticMeshComponent(None)]
        world_loc = loc or _Vec()
        actor = _Actor(self, name, cls.__name__, comps,
                       (world_loc.x, world_loc.y, world_loc.z))
        self.spawned_classes.append(cls.__name__)
        self.actors.append(actor)
        return actor


class _SysLib(object):
    def __init__(self, saved_dir):
        self._saved = saved_dir

    def get_project_saved_directory(self):
        return self._saved

    def get_project_directory(self):
        return self._saved.rsplit("/", 1)[0]

    def get_components_by_class(self, actor, cls):
        return actor.get_components_by_class(cls)


class _AssetLib(object):
    def __init__(self, world_meshes):
        self._meshes = world_meshes

    def load_asset(self, path):
        return _MeshObj(path)


class _SaveUtils(object):
    calls = []

    @staticmethod
    def save_dirty_map_packages(packages, only_dirty):
        _SaveUtils.calls.append("save_dirty_map_packages")


def _make_unreal(world, tmp_path):
    mod = types.ModuleType("unreal")
    mod.Vector = _Vec
    mod.Rotator = _Rot
    mod.StaticMeshComponent = StaticMeshComponent
    mod.HierarchicalStaticMeshComponent = HierarchicalStaticMeshComponent
    mod.InstancedStaticMeshComponent = InstancedStaticMeshComponent
    mod.ActorComponent = ActorComponent
    mod.HierarchicalStaticMeshActor = HierarchicalStaticMeshActor
    mod.StaticMeshActor = StaticMeshActor
    mod.SystemLibrary = _SysLib(str(tmp_path).replace("\\", "/"))
    mod.EditorAssetLibrary = _AssetLib(None)
    mod.EditorLoadingAndSavingUtils = _SaveUtils
    mod.BuildVersion = lambda: "5.4.4-fake"

    class _ELL(object):
        @staticmethod
        def get_editor_world():
            return world

        @staticmethod
        def get_all_level_actors():
            return list(world.actors)

        @staticmethod
        def spawn_actor_from_class(cls, loc, rot):
            return world.spawn(cls, loc, rot)

        @staticmethod
        def destroy_actor(actor):
            return actor.destroy()

        @staticmethod
        def save_current_level():
            _SaveUtils.calls.append("save_current_level")

    class _EAS(object):
        pass

    class _LES(object):
        pass

    mod.EditorLevelLibrary = _ELL
    mod.EditorActorSubsystem = _EAS
    mod.LevelEditorSubsystem = _LES

    def get_editor_subsystem(cls):
        if cls is _EAS:
            class _Proxy(object):
                @staticmethod
                def get_all_level_actors():
                    return list(world.actors)

                @staticmethod
                def spawn_actor_from_class(cls2, loc, rot):
                    return world.spawn(cls2, loc, rot)

                @staticmethod
                def destroy_actor(actor):
                    return actor.destroy()
            return _Proxy()
        if cls is _LES:
            class _Proxy2(object):
                @staticmethod
                def get_editor_world():
                    return world
            return _Proxy2()
        return None

    mod.get_editor_subsystem = get_editor_subsystem
    return mod


def _seed_world(cube=7, sphere=3, overrides=()):
    world = _World()
    for i in range(cube):
        world.actors.append(_Actor(world, "Cube_%d" % i, "StaticMeshActor",
                                   [StaticMeshComponent(_MeshObj(CUBE),
                                                        1 if "Cube_%d" % i in overrides else 0)],
                                   (float(i), 0.0, 0.0)))
    for i in range(sphere):
        world.actors.append(_Actor(world, "Sph_%d" % i, "StaticMeshActor",
                                   [StaticMeshComponent(_MeshObj(OTHER))],
                                   (0.0, float(i), 0.0)))
    return world


@pytest.fixture()
def fake_engine(monkeypatch, tmp_path):
    world = _seed_world()
    monkeypatch.setitem(sys.modules, "unreal", _make_unreal(world, tmp_path))
    _SaveUtils.calls = []
    return world


def test_plan_on_engine_reports_the_group(fake_engine):
    res = oh.opt_collapse_hism(min_count=5, limit=10)["result"]
    assert res["universe"] == 10 and res["scanned"] == 10
    assert res["total"] == 1 and res["planned"] == 6
    assert res["groups"][0]["actors"] == 7
    assert res["dry_run"] is True and res["executed"] is False
    assert len(fake_engine.actors) == 10, "plan 不得动场景"
    assert _SaveUtils.calls == []


def test_collapse_writes_hism_manifest_and_destroys_sources(fake_engine, tmp_path):
    manifest = tmp_path / "Saved" / "Prism" / "roll.json"
    res = oh.opt_collapse_hism(mode="collapse", min_count=5, limit=10,
                               manifest=str(manifest),
                               dry_run=False, confirm=True)["result"]
    assert res["executed"] is True and res["success"] is True, res["note"]
    assert res["hism_created"] == 1 and res["actors_destroyed"] == 7
    assert res["actors_instanced"] == 7
    assert res["drawcalls_before"] == 7 and res["drawcalls_after"] == 1
    assert res["level_saved"] is True and _SaveUtils.calls
    assert res["rollback_available"] is True and manifest.exists()
    assert "EditorLevelLibrary.spawn_actor_from_class" in res["tried"]
    assert "add_instances" in res["tried"]
    kinds = [a.get_class().get_name() for a in fake_engine.actors]
    assert kinds.count("HierarchicalStaticMeshActor") == 1
    assert kinds.count("StaticMeshActor") == 3, "只折叠 Cube，Sphere 组不够阈值"
    hism = [a for a in fake_engine.actors if a._class == "HierarchicalStaticMeshActor"][0]
    comp = hism._comps[0]
    assert comp.static_mesh.package == CUBE
    assert len(comp.instances) == 7
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert payload["op"] == oh.OP and payload["version"] == oh.MANIFEST_VERSION
    assert len(payload["groups"]) == 1 and len(payload["groups"][0]["sources"]) == 7
    assert payload["groups"][0]["sources"][0]["mesh"] == CUBE
    assert payload["groups"][0]["hism_actor"] == hism.get_name()


def test_restore_from_manifest_round_trips_the_actors(fake_engine, tmp_path):
    manifest = tmp_path / "roll.json"
    oh.opt_collapse_hism(mode="collapse", min_count=5, limit=10,
                         manifest=str(manifest), dry_run=False, confirm=True)
    after_collapse = len(fake_engine.actors)
    plan = oh.opt_collapse_hism(mode="restore", manifest=str(manifest))["result"]
    assert plan["planned"] == 7 and plan["executed"] is False
    assert len(fake_engine.actors) == after_collapse, "restore 的 dry_run 也不许动场景"
    res = oh.opt_collapse_hism(mode="restore", manifest=str(manifest),
                               dry_run=False, confirm=True)["result"]
    assert res["success"] is True and res["changed"] == 7
    assert res["actors_destroyed"] == 1
    kinds = [a.get_class().get_name() for a in fake_engine.actors]
    assert kinds.count("StaticMeshActor") == 10
    assert kinds.count("HierarchicalStaticMeshActor") == 0
    labels = sorted(a.get_actor_label() for a in fake_engine.actors
                    if a._class == "StaticMeshActor")
    assert "Cube_0" in labels, "对象名由引擎重新分配，身份靠 label 回写（诚实口径）"
    assert labels.count("Sph_0") == 1
    for actor in fake_engine.actors:
        if actor._class == "StaticMeshActor" and actor.get_actor_label() == "Cube_3":
            assert actor.get_actor_location().x == 3.0
            comp = actor._comps[0]
            assert comp is not None and comp.static_mesh.package == CUBE


def test_restore_rejects_a_broken_manifest(fake_engine, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    res = oh.opt_collapse_hism(mode="restore", manifest=str(bad),
                               dry_run=False, confirm=True)["result"]
    assert res["executed"] is False and "清单" in res["note"]


def test_collapse_refuses_when_hism_component_is_unreachable(monkeypatch, tmp_path):
    world = _seed_world()
    mod = _make_unreal(world, tmp_path)

    def broken_component(actor, cls):
        raise AttributeError("no component in this engine build")

    monkeypatch.setattr(oh, "_hism_component", lambda u, a, t: None)
    monkeypatch.setitem(sys.modules, "unreal", mod)
    res = oh.opt_collapse_hism(mode="collapse", min_count=5, limit=10,
                               manifest=str(tmp_path / "m.json"),
                               dry_run=False, confirm=True)["result"]
    assert res["success"] is False and res["hism_created"] == 0
    assert res["failed"] and res["failed"][0]["note"] == "hism_component_missing"
