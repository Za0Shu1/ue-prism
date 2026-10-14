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
from prism.domain import opt_instancing as oi

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


class _Slot(object):
    def __init__(self, mat):
        self.material_interface = mat


class _MeshObj(object):
    """网格替身。declares_none=True 模拟引擎 BasicShapes：槽位声明为 None，
    但组件解析时会被代入默认材质（真实误判源头）。"""

    def __init__(self, package, default_mat=None, declares_none=False):
        self.package = package
        self.default_mat = default_mat or _MeshMat("/Engine/EngineMaterials/DefaultMaterial")
        self.static_materials = [_Slot(None if declares_none else self.default_mat)]
        self._declares_none = declares_none

    def get_path_name(self):
        return self.package + "." + self.package.rsplit("/", 1)[-1]

    def get_num_sections(self, lod):
        return 1

    def get_material(self, index):
        return None if self._declares_none else self.default_mat


class _MeshMat(object):
    def __init__(self, package):
        self.package = package

    def get_path_name(self):
        return self.package + "." + self.package.rsplit("/", 1)[-1]


class ActorComponent(object):
    """真机里所有组件都派生自 ActorComponent，探测按它兜底。"""

    def __init__(self):
        self._props = {"mobility": "Movable", "b_cast_shadow": True,
                       "collision_profile_name": "BlockAll"}
        self.modified = False

    def get_editor_property(self, name):
        if name in self._props:
            return self._props[name]
        if hasattr(self, name):
            return getattr(self, name)
        raise AttributeError(name)

    def set_editor_property(self, name, value):
        if name in self._props or not hasattr(self, name):
            self._props[name] = value
            return
        setattr(self, name, value)

    def modify(self):
        self.modified = True


class StaticMeshComponent(ActorComponent):
    """组件替身：override_materials 是 5.4 的权威覆盖数组；get_material(i) 返回解析值。

    expose_overrides=False 模拟读不到数组的旧/漂移签名，逼出"同口径比对"兜底。
    """

    def __init__(self, mesh, override=None, expose_overrides=True,
                 resolved_default=None, prefill_default=False,
                 prefill_mat=None):
        ActorComponent.__init__(self)
        self.static_mesh = mesh
        self._override = override
        self.resolved_default = resolved_default
        if expose_overrides:
            # 5.4 真机实况：编辑器会把网格自身材质预填进覆盖数组，非空 != 有偏离
            filled = override if override is not None else (
                prefill_mat if prefill_mat is not None else (
                    mesh.default_mat if prefill_default else None))
            self.override_materials = [filled]

    def get_class(self):
        return _Cls("StaticMeshComponent")

    def get_num_materials(self):
        return 1

    def get_material(self, index):
        if self._override is not None:
            return self._override
        if self.resolved_default is not None:
            return self.resolved_default
        return self.static_mesh.default_mat if self.static_mesh else None

    def get_materials(self):
        return [self.get_material(0)]


class InstancedStaticMeshComponent(ActorComponent):
    """真机 UInstancedStaticMeshComponent：HISM 的基类，按 ISM 探测能命中 HISM。"""

    def __init__(self):
        ActorComponent.__init__(self)
        self.static_mesh = None
        self.instances = []
        self.added_via = None
        self._object_name = ""

    def get_class(self):
        return _Cls(type(self).__name__)

    def add_instances(self, transforms, b_world_space=None):
        if b_world_space is None:
            # 5.4 实测：单参数批接口直接 TypeError，绑定的是 (transforms, bWorldSpace)
            raise TypeError("add_instances() requires a bool")
        self.instances.extend(transforms)
        self.added_via = "add_instances(%s)" % b_world_space
        return []   # 5.4 实测：能写但返回空列表，写入计数不能信返回值

    def add_instance(self, transform, b_world_space=None):
        self.instances.append(transform)
        self.added_via = "add_instance"
        return len(self.instances) - 1

    def get_instance_count(self):
        return len(self.instances)

    def set_static_mesh(self, mesh):
        self.static_mesh = mesh


class HierarchicalInstancedStaticMeshComponent(InstancedStaticMeshComponent):
    """5.4 真名（dir(unreal) 实测）：HISM 组件是这个名字。"""


class HierarchicalStaticMeshComponent(HierarchicalInstancedStaticMeshComponent):
    """旧猜测名：5.4 不存在，只为"真有该名的版本"保留兜底路线。"""


FAKE_HISM_COMPS = {"HierarchicalInstancedStaticMeshComponent":
                   HierarchicalInstancedStaticMeshComponent,
                   "HierarchicalStaticMeshComponent": HierarchicalStaticMeshComponent}


class Actor(object):
    """5.4 真机没有 HISM/ISM 的 Actor 子类；引擎自己的实例化工具用的就是裸 AActor。"""


class HierarchicalStaticMeshActor(Actor):
    """假想中的专用容器类：只有真绑了它的版本才走这条路线。"""


class StaticMeshActor(Actor):
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
        self.modified = False
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
        return [c for c in self._comps if isinstance(c, cls)]

    def get_component_by_class(self, cls):
        for comp in self._comps:
            if isinstance(comp, cls):
                return comp
        raise AttributeError(cls.__name__)

    def set_editor_property(self, name, value):
        # 5.4 实测：add_component_by_class 没进脚本绑定，挂根组件只能走 root_component
        if name == "root_component":
            if value not in self._comps:
                self._comps.append(value)
            return
        setattr(self, name, value)

    def get_editor_property(self, name):
        if name == "root_component":
            return self._comps[0] if self._comps else None
        if name == "components":
            return list(self._comps)
        return getattr(self, name)

    def set_root_component(self, comp):
        if comp not in self._comps:
            self._comps.append(comp)

    def modify(self):
        self.modified = True

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
        self.hism_comp_names = ("HierarchicalInstancedStaticMeshComponent",)

    def spawn(self, cls, loc=None, rot=None):
        name = "%s_%d" % (cls.__name__, len(self.actors))
        self.counter += 1
        comps = []
        if cls.__name__ in ("HierarchicalStaticMeshActor", "InstancedStaticMeshActor"):
            # 真有专用 Actor 类的版本：自带实例组件，spawn 完直接能用
            comps = [FAKE_HISM_COMPS[self.hism_comp_names[0]]()]
        elif cls.__name__ == "StaticMeshActor":
            comps = [StaticMeshComponent(None)]
        # 裸 Actor：真机语义是"什么组件都没有"，必须自己 new_object 再挂 root
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


def _new_object(cls, outer=None, name=""):
    """真机同语义：new_object 只造对象，绝不自动挂到 outer——漏挂 root 必须被抓住。"""
    comp = cls()
    if name:
        comp._object_name = name
    return comp


def _make_unreal(world, tmp_path, hism_actor_class=False,
                 comp_names=("HierarchicalInstancedStaticMeshComponent",)):
    """假引擎默认复刻 5.4 真机：没有 HISM/ISM 的 Actor 类，组件真名是
    HierarchicalInstancedStaticMeshComponent。hism_actor_class=True 测"真有专用
    容器类"的版本；comp_names 测旧猜测名兜底，以及 "() = 什么都没绑"的诚实失败。
    """
    mod = types.ModuleType("unreal")
    world.hism_comp_names = tuple(comp_names) or ("HierarchicalInstancedStaticMeshComponent",)
    mod.Vector = _Vec
    mod.Rotator = _Rot
    mod.StaticMeshComponent = StaticMeshComponent
    mod.InstancedStaticMeshComponent = InstancedStaticMeshComponent
    mod.ActorComponent = ActorComponent
    mod.Actor = Actor
    mod.StaticMeshActor = StaticMeshActor
    for _comp_name in comp_names:
        setattr(mod, _comp_name, FAKE_HISM_COMPS[_comp_name])
    if hism_actor_class:
        mod.HierarchicalStaticMeshActor = HierarchicalStaticMeshActor
    mod.new_object = _new_object
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


def _seed_world(cube=7, sphere=3, overrides=(), expose_overrides=True,
                declares_none=False, resolved_default=None, prefill_default=False,
                prefill_mat=None, mesh_default=None):
    world = _World()
    for i in range(cube):
        override = _MeshMat("/Game/Mats/M_Different") if "Cube_%d" % i in overrides else None
        world.actors.append(_Actor(world, "Cube_%d" % i, "StaticMeshActor",
                                   [StaticMeshComponent(_MeshObj(CUBE, default_mat=mesh_default,
                                           declares_none=declares_none),
                                                        override,
                                                        expose_overrides=expose_overrides,
                                                        resolved_default=resolved_default,
                                                        prefill_default=prefill_default,
                                                        prefill_mat=prefill_mat)],
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
    """5.4 真机路线：裸 Actor + new_object 出的 HISM 根组件（引擎里没有专用 Actor 类）。"""
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
    assert "add_instances:world" in res["tried"]
    # 真机证据链：专用 Actor 类确实不存在，才有下面的裸 Actor 路线
    assert "class:HierarchicalStaticMeshActor:missing" in res["tried"]
    assert "class:InstancedStaticMeshActor:missing" in res["tried"]
    assert ("new_object_hism:named(HierarchicalInstancedStaticMeshComponent)"
            in res["tried"])
    assert "root_component:set_editor_property(root_component)" in res["tried"]
    assert res["groups"][0]["spawn_path"] == "plain_actor+new_object"
    kinds = [a.get_class().get_name() for a in fake_engine.actors]
    assert kinds.count("StaticMeshActor") == 3, "只折叠 Cube，Sphere 组不够阈值"
    containers = _hism_comps(fake_engine)
    assert len(containers) == 1
    hism, comp = containers[0]
    assert hism.get_class().get_name() == "Actor"
    assert hism._comps == [comp] and comp._object_name == oh._HISM_COMP_NAME
    assert comp.static_mesh.package == CUBE
    assert len(comp.instances) == 7 and comp.get_instance_count() == 7
    assert comp.modified is True, "改了组件不 modify，编辑器不会把它标脏"
    assert hism.get_actor_label() == "Cube_HISM"
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert payload["op"] == oh.OP and payload["version"] == oh.MANIFEST_VERSION
    assert len(payload["groups"]) == 1 and len(payload["groups"][0]["sources"]) == 7
    assert payload["groups"][0]["sources"][0]["mesh"] == CUBE
    assert payload["groups"][0]["hism_actor"] == hism.get_name()
    assert payload["groups"][0]["hism_class"] == "plain_actor+new_object"


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
    assert _hism_comps(fake_engine) == [], "回滚后不该再有任何实例化容器"
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


def test_override_gate_uses_the_real_override_array_not_resolved_slots(monkeypatch, tmp_path):
    """静态替身钉死 5.4 真机踩过的四个判等口径：宁可放行也不许误伤。

    1) override_materials=[None]            -> 0 覆盖，放行
    2) override_materials=[别的材质]         -> 1 覆盖，整组拒收
    3) 读不到覆盖数组、组件解析值与网格材质同 -> 0 覆盖，放行
    4) 网格槽声明 None、解析代入引擎默认材质   -> 同一个默认外观，0 覆盖（5.4 引擎 Cube）
    5) 覆盖数组预填 WorldGridMaterial、网格侧给 BasicShapeMaterial
       -> 两条路径一个外观，0 覆盖（1500 Cube 被误判的实况）
    6) 同包裹不同导出名（_INST_ 变体）        -> 0 覆盖
    7) 基线取不到且候选不是引擎默认材质        -> 判不准：走 material_override_unknown 放行
    """
    clean = _seed_world(cube=6)
    monkeypatch.setitem(sys.modules, "unreal", _make_unreal(clean, tmp_path))
    res = oh.opt_collapse_hism(min_count=5, limit=5)["result"]
    assert res["total"] == 1 and res["material_override_refused"] == 0
    assert res["groups"][0]["material_overrides"] == 0
    assert "override_source:override_materials" in res["tried"]

    dirty = _seed_world(cube=6, overrides={"Cube_2"})
    monkeypatch.setitem(sys.modules, "unreal", _make_unreal(dirty, tmp_path))
    res2 = oh.opt_collapse_hism(min_count=5, limit=5)["result"]
    assert res2["total"] == 0 and res2["material_override_refused"] == 1
    assert res2["refused_groups"][0]["reason"] == "material_overrides"
    assert res2["refused_groups"][0]["material_overrides"] == 1
    assert any("override_mismatch:" in t for t in res2["tried"]), "真偏离要留证据"

    same = _seed_world(cube=6, expose_overrides=False)
    monkeypatch.setitem(sys.modules, "unreal", _make_unreal(same, tmp_path))
    res3 = oh.opt_collapse_hism(min_count=5, limit=5)["result"]
    assert res3["total"] == 1 and "override_source:comp.get_material" in res3["tried"]

    engine_cube = _seed_world(cube=6, expose_overrides=False, declares_none=True,
                              resolved_default=_MeshMat("/Engine/EngineMaterials/WorldGridMaterial"))
    monkeypatch.setitem(sys.modules, "unreal", _make_unreal(engine_cube, tmp_path))
    res4 = oh.opt_collapse_hism(min_count=5, limit=5)["result"]
    assert res4["total"] == 1, "引擎默认材质不是作者覆盖，不得拒收整组"
    assert res4["refused_groups"] == []
    assert res4["groups"][0]["material_overrides"] == 0

    prefilled = _seed_world(cube=6, prefill_default=True)
    monkeypatch.setitem(sys.modules, "unreal", _make_unreal(prefilled, tmp_path))
    res5 = oh.opt_collapse_hism(min_count=5, limit=5)["result"]
    assert res5["total"] == 1, "网格自身材质被预填进覆盖数组时不算作者覆盖（真实 1500 Cube 实况）"
    assert res5["refused_groups"] == []

    two_defaults = _seed_world(
        cube=6, prefill_mat=_MeshMat("/Engine/EngineMaterials/WorldGridMaterial"),
        mesh_default=_MeshMat("/Engine/BasicShapes/BasicShapeMaterial"))
    monkeypatch.setitem(sys.modules, "unreal", _make_unreal(two_defaults, tmp_path))
    res6 = oh.opt_collapse_hism(min_count=5, limit=5)["result"]
    assert res6["total"] == 1, "两条引擎默认材质路径是同一个外观，不得判成覆盖"
    assert res6["groups"][0]["material_overrides"] == 0

    class _Export(object):
        def __init__(self, package, export):
            self.package = package
            self.export = export

        def get_path_name(self):
            return self.package + "." + self.export

    variants = _seed_world(cube=6,
                           prefill_mat=_Export("/Game/Mats/M_Rock", "M_Rock_INST_1"),
                           mesh_default=_Export("/Game/Mats/M_Rock", "M_Rock"))
    monkeypatch.setitem(sys.modules, "unreal", _make_unreal(variants, tmp_path))
    res7 = oh.opt_collapse_hism(min_count=5, limit=5)["result"]
    assert res7["total"] == 1, "同包裹不同导出名是同一个材质，不得判成覆盖"

    blind = _seed_world(cube=6, expose_overrides=False, declares_none=True,
                        resolved_default=_MeshMat("/Game/Mats/M_SlotDefault"))
    monkeypatch.setitem(sys.modules, "unreal", _make_unreal(blind, tmp_path))
    res8 = oh.opt_collapse_hism(min_count=5, limit=5)["result"]
    assert res8["total"] == 1, "基线读不到时判不准，走 unknown 记账放行，不当覆盖拒收"
    assert res8["material_override_unknown"] == 6
    assert any("baseline_unreadable" in t for t in res8["tried"])

def _hism_comps(world):
    """场景里挂好的实例化容器（裸 Actor + HISM 组件）：折了几个、留了半个，一目了然。"""
    out = []
    for actor in world.actors:
        for comp in actor._comps:
            if isinstance(comp, InstancedStaticMeshComponent):
                out.append((actor, comp))
    return out


def test_collapse_reports_the_route_when_no_hism_is_reachable(monkeypatch, tmp_path):
    """容器造不出来时整组不动：路线写进 failed.path，不许假成功、不许丢 Actor。"""
    world = _seed_world()
    mod = _make_unreal(world, tmp_path)
    monkeypatch.setattr(oh, "_attach_hism_component", lambda u, a, t: None)
    monkeypatch.setitem(sys.modules, "unreal", mod)
    res = oh.opt_collapse_hism(mode="collapse", min_count=5, limit=10,
                               manifest=str(tmp_path / "m.json"),
                               dry_run=False, confirm=True)["result"]
    assert res["success"] is False and res["hism_created"] == 0
    assert res["failed"][0]["note"] == "spawn_hism_failed"
    assert res["failed"][0]["path"] == "plain_actor:attach_failed"
    assert len(world.actors) == 10, "造不出容器就不该动任何一个源 Actor"
    assert not (tmp_path / "m.json").exists(), "一组都没折成，不得落回滚清单"


def test_collapse_is_a_no_op_when_the_engine_exposes_no_hism_component(monkeypatch, tmp_path):
    """5.4 实测过的最坏情况：引擎连 HISM 组件类都没绑 -> 如实报 missing，不硬造。"""
    world = _seed_world()
    mod = _make_unreal(world, tmp_path, comp_names=())
    monkeypatch.setitem(sys.modules, "unreal", mod)
    res = oh.opt_collapse_hism(mode="collapse", min_count=5, limit=10,
                               manifest=str(tmp_path / "m.json"),
                               dry_run=False, confirm=True)["result"]
    assert res["success"] is False and res["hism_created"] == 0
    assert "hism_comp_class:missing" in res["tried"]
    assert res["failed"][0]["path"] == "plain_actor:attach_failed"
    assert len(world.actors) == 10


def test_collapse_uses_a_dedicated_hism_actor_when_the_engine_has_one(monkeypatch, tmp_path):
    """真有 AHierarchicalStaticMeshActor 的版本：走专用类，不该退到裸 Actor 路线。"""
    world = _seed_world()
    mod = _make_unreal(world, tmp_path, hism_actor_class=True)
    monkeypatch.setitem(sys.modules, "unreal", mod)
    res = oh.opt_collapse_hism(mode="collapse", min_count=5, limit=10,
                               manifest=str(tmp_path / "d.json"),
                               dry_run=False, confirm=True)["result"]
    assert res["success"] is True, res["note"]
    assert res["groups"][0]["spawn_path"] == "actor_class:HierarchicalStaticMeshActor"
    assert "hism_component:by_class" in res["tried"]
    assert not any(t.startswith("new_object_hism") for t in res["tried"])
    assert world.spawned_classes.count("HierarchicalStaticMeshActor") == 1
    payload = json.loads((tmp_path / "d.json").read_text(encoding="utf-8"))
    assert payload["groups"][0]["hism_class"] == "actor_class:HierarchicalStaticMeshActor"


def test_collapse_falls_back_to_the_legacy_component_name(monkeypatch, tmp_path):
    """旧猜测名兜底：只绑了 HierarchicalStaticMeshComponent 的版本也要能折。"""
    world = _seed_world()
    mod = _make_unreal(world, tmp_path, comp_names=("HierarchicalStaticMeshComponent",))
    monkeypatch.setitem(sys.modules, "unreal", mod)
    res = oh.opt_collapse_hism(mode="collapse", min_count=5, limit=10,
                               manifest=str(tmp_path / "l.json"),
                               dry_run=False, confirm=True)["result"]
    assert res["success"] is True, res["note"]
    assert "new_object_hism:named(HierarchicalStaticMeshComponent)" in res["tried"]
    assert res["groups"][0]["spawn_path"] == "plain_actor+new_object"


def test_collapse_carries_shadow_and_collision_from_the_source(monkeypatch, tmp_path):
    """尽力保真：源组件的阴影/可移动性/碰撞抄给新组件，抄不到的如实记账。"""
    world = _seed_world()
    src = world.actors[0]._comps[0]
    src.set_editor_property("b_cast_shadow", False)
    src.set_editor_property("mobility", "Stationary")
    monkeypatch.setitem(sys.modules, "unreal", _make_unreal(world, tmp_path))
    res = oh.opt_collapse_hism(mode="collapse", min_count=5, limit=10,
                               manifest=str(tmp_path / "f.json"),
                               dry_run=False, confirm=True)["result"]
    assert res["success"] is True, res["note"]
    comp = _hism_comps(world)[0][1]
    assert comp.get_editor_property("b_cast_shadow") is False
    assert comp.get_editor_property("mobility") == "Stationary"
    assert "fidelity:b_cast_shadow" in res["tried"]
    assert "fidelity:mobility" in res["tried"]


def test_collapse_refuses_when_instances_are_silently_dropped(monkeypatch, tmp_path):
    """add_instances 返回空列表还悄悄少写：只信 get_instance_count()，写不满就整组退回。"""
    world = _seed_world()
    mod = _make_unreal(world, tmp_path)
    real_new_object = mod.new_object

    def short_write(cls, outer=None, name=""):
        comp = real_new_object(cls, outer, name)
        original = comp.add_instances
        comp.add_instances = lambda transforms, b_world_space=None: original(
            transforms[:1], b_world_space)
        return comp

    mod.new_object = short_write
    monkeypatch.setitem(sys.modules, "unreal", mod)
    res = oh.opt_collapse_hism(mode="collapse", min_count=5, limit=10,
                               manifest=str(tmp_path / "s.json"),
                               dry_run=False, confirm=True)["result"]
    assert res["success"] is False and res["hism_created"] == 0
    assert res["failed"][0]["note"] == "instance_write_short"
    assert res["failed"][0]["added"] == 1 and res["failed"][0]["wanted"] == 7
    assert len(world.actors) == 10, "实例写不满还销毁源 = 丢几何，必须整组退回"
    assert _hism_comps(world) == [], "退回时不得留下半个容器"


def test_collapse_refuses_when_a_source_transform_is_unreadable(monkeypatch, tmp_path):
    """读不到某个源的世界变换也不能折：宁可整组留在原地。"""
    world = _seed_world()
    world.actors[2].get_actor_transform = lambda: (_ for _ in ()).throw(
        RuntimeError("transform gone"))
    monkeypatch.setitem(sys.modules, "unreal", _make_unreal(world, tmp_path))
    res = oh.opt_collapse_hism(mode="collapse", min_count=5, limit=10,
                               manifest=str(tmp_path / "t.json"),
                               dry_run=False, confirm=True)["result"]
    assert res["success"] is False and res["hism_created"] == 0
    assert res["failed"][0]["note"] == "transform_unreadable"
    assert len(world.actors) == 10
    assert _hism_comps(world) == [], "整组退回时不得留下半个容器"


def test_add_instances_prefers_world_space_and_trusts_only_the_count():
    tried = []
    comp = HierarchicalInstancedStaticMeshComponent()
    transform = _Tr(_Vec(1.0, 0.0, 0.0), _Rot(), _Vec(1.0, 1.0, 1.0))
    assert oh._add_instances(comp, [transform], tried) == 1
    assert comp.added_via == "add_instances(True)", "5.4 单参数批接口会 TypeError，必须给 bool"
    assert "add_instances:world" in tried
    assert tried.count("add_instances:world") == 1, "成功路径不该把 tried 刷成流水账"
    assert oh._add_instances(comp, [], tried) == 0


def test_add_instances_falls_back_to_local_space_then_per_instance():
    transform = _Tr(_Vec(2.0, 0.0, 0.0), _Rot(), _Vec(1.0, 1.0, 1.0))

    tried = []
    comp = HierarchicalInstancedStaticMeshComponent()

    def no_world(transforms, b_world_space=None):
        if b_world_space is True:
            raise TypeError("no world space in this build")
        comp.instances.extend(transforms)
        comp.added_via = "local"
        return []

    comp.add_instances = no_world
    assert oh._add_instances(comp, [transform], tried) == 1
    assert comp.added_via == "local"
    assert "add_instances_world:TypeError" in tried
    assert "add_instances:local" in tried

    tried2 = []
    comp2 = HierarchicalInstancedStaticMeshComponent()

    def no_batch(*args):
        raise AttributeError("no batch api")

    comp2.add_instances = no_batch
    assert oh._add_instances(comp2, [transform, transform], tried2) == 2
    assert comp2.added_via == "add_instance"
    assert comp2.get_instance_count() == 2
    assert "add_instance(loop)" in tried2


def test_restore_writes_the_mesh_into_the_component(fake_engine, tmp_path):
    """回滚的真值不是"Actor 数量回来了"，而是"网格还在原来的位置上"。"""
    manifest = tmp_path / "roll.json"
    oh.opt_collapse_hism(mode="collapse", min_count=5, limit=10,
                         manifest=str(manifest), dry_run=False, confirm=True)
    res = oh.opt_collapse_hism(mode="restore", manifest=str(manifest),
                               dry_run=False, confirm=True)["result"]
    assert res["success"] is True, res["note"]
    assert res["restore_mesh_missing"] == 0
    assert res.get("failed_truncated") is False
    cubes = [a for a in fake_engine.actors
             if a._class == "StaticMeshActor" and a.get_actor_label().startswith("Cube")]
    assert len(cubes) == 7
    for actor in cubes:
        assert actor._comps and actor._comps[0].static_mesh is not None
        assert actor._comps[0].static_mesh.package == CUBE


def test_restore_reports_meshless_actors_instead_of_a_fake_success(monkeypatch, tmp_path):
    """复刻 5.4 踩过的坑：往 AActor 上丢个 static_mesh 也算"没报错"，但场景里是
    一堆空壳。只认回读，抓得住、也不得把信封刷成 1500 行。"""
    world = _seed_world(cube=25, sphere=0)
    mod = _make_unreal(world, tmp_path)
    monkeypatch.setattr(oh, "_mesh_component", lambda u, a: None)
    monkeypatch.setattr(
        _Actor, "set_static_mesh",
        lambda self, mesh: (_ for _ in ()).throw(AttributeError("no such method")))
    monkeypatch.setitem(sys.modules, "unreal", mod)
    manifest = tmp_path / "broken.json"
    oh.opt_collapse_hism(mode="collapse", min_count=5, limit=10,
                         manifest=str(manifest), dry_run=False, confirm=True)
    assert len(world.actors) == 1, "先让它真折叠成一个容器"
    res = oh.opt_collapse_hism(mode="restore", manifest=str(manifest),
                               dry_run=False, confirm=True)["result"]
    assert res["changed"] == 25 and len(world.actors) == 25
    assert res["restore_mesh_missing"] == 25
    assert res["success"] is False, "空壳回滚不得报 success"
    assert len(res["failed"]) == 20 and res["failed_truncated"] is True
    assert res["failed"][0]["note"] == "mesh_not_applied"
    assert "警告" in res["note"]
    assert any(t.startswith("restore_set_mesh:") for t in res["tried"])



def test_analyze_counts_the_container_it_just_made(fake_engine, tmp_path):
    """折完要能自证：容器是裸 Actor + HISM，类名里没有 Instanced。

    早先它会被漏判成"无网格 Actor"，合批账数不到自己头上，用户无法用
    analyze_instancing 验收"折过了"。
    """
    before = oi.analyze_instancing(min_count=5, cap=10)["summary"]
    assert before["plain_staticmesh_actors"] == 10
    manifest = tmp_path / "acc.json"
    col = oh.opt_collapse_hism(mode="collapse", min_count=5, limit=10,
                               manifest=str(manifest),
                               dry_run=False, confirm=True)["result"]
    assert col["success"] is True, col["note"]
    after = oi.analyze_instancing(min_count=5, cap=10)["summary"]
    assert after["plain_staticmesh_actors"] == 3, "Sphere 组不够阈值，仍是散装"
    assert after["groups_total"] == 0 and after["collapsible_actors"] == 0
    assert after["excluded_instanced_actors"] == 1, "\u5bb9\u5668\u8981\u7b97\u5df2\u5b9e\u4f8b\u5316"
    assert after["excluded_no_mesh_actors"] == 0, "\u4e0d\u5f97\u628a\u5bb9\u5668\u6f0f\u6210\u65e0\u7f51\u683c Actor"
