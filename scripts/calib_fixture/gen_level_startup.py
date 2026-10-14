# -*- coding: utf-8 -*-
"""夹具关卡生成（GUI 编辑器 StartupScripts 入口，非 commandlet）。

5.4 实证：new_level/关卡编辑/TextureCube 复制在 -run=pythonscript 下会崩，
必须走 GUI 编辑器启动脚本。本脚本幂等：地图已布满则只补 TextureCube 与计数。

启用（二选一）:
  1) run_fixture.py 自动注入并还原 DefaultEngine.ini（推荐）
  2) 手动: [/Script/PythonScriptPlugin.PythonScriptPluginSettings]
     +StartupScripts="<绝对路径>/gen_level_startup.py"，然后
     UnrealEditor.exe <uproject> - 启动后自动执行
产物标记: <PRISM_FIXTURE_TMP>/done.json（或 <工程>/Saved/PrismFixture/done.json）
"""
import json
import os
import traceback

import unreal

EAL = unreal.EditorAssetLibrary
ELL = unreal.EditorLevelLibrary
FOLDER = "/Game/PrismCalib"
MAP = "CalibMap"
SPAWN_N = int(os.environ.get("PRISM_FIXTURE_ACTORS", "1500"))
# 幂等只看数量会把"回滚后网格丢光"的地图留下来：PRISM_FIXTURE_RESET_MAP=1
# 先删掉关卡重建，让真值始终是"带网格的 1500 个散装 Cube"。
RESET_MAP = os.environ.get("PRISM_FIXTURE_RESET_MAP", "") == "1"
MESH = "/Engine/BasicShapes/Cube.Cube"


def _tmp_dir():
    d = os.environ.get("PRISM_FIXTURE_TMP")
    if not d:
        d = os.path.join(str(unreal.SystemLibrary.get_project_directory()),
                         "Saved", "PrismFixture")
    return d


def _set_compression(tex, member, out, key):
    """把压缩设置写成指定枚举成员并回读（跨版本成员名漂移多候选）。

    写失败必须记进 out 让 checks 当场响：靶的压缩格式没造上，
    下游所有格式判定都会变成"看着对其实空转"。
    """
    enum = getattr(unreal, "TextureCompressionSettings", None)
    errs = []
    for name in (member, member.replace("TC_", "")):
        value = getattr(enum, name, None) if enum is not None else None
        if value is None:
            errs.append(name + ":no_member")
            continue
        try:
            tex.set_editor_property("compression_settings", value)
        except Exception as exc:
            errs.append(name + ":" + type(exc).__name__)
            continue
        got = str(tex.get_editor_property("compression_settings"))
        out[key] = {"set": name, "readback": got, "errs": errs}
        return member in got or got.endswith(member)
    out[key] = {"set": None, "errs": errs}
    return False


def make_cube(out, name="T_CalibCube", hdr=False):
    """TextureCube 靶：引擎自带立方图复制一份（GUI 下可行；commandlet 会崩）。

    hdr=True 再叠 TC_HDR，作为"高位深不降块压缩"的真值靶：5.4 真机定案
    （/Game/ClientLib/Material/Textures/T_HDR 常驻 201326592 = 6x2048^2x8B），
    旧口径把 TC_HDR 归"压缩格式"按 1B/px 估 -> 池账低估 6 倍且从不报账。
    幂等：资产已存在也要确认压缩格式真的是 TC_HDR（上一轮可能没写上）。
    """
    key = "hdr_cube" if hdr else "cube"
    dest = FOLDER + "/" + name
    disk = os.path.join(str(unreal.SystemLibrary.get_project_directory()),
                        "Content", "PrismCalib", name + ".uasset")
    if os.path.isfile(disk) and EAL.does_asset_exist(dest):
        if not hdr:
            out[key] = "exists"
            return
        exist_tex = unreal.load_asset(dest)
        if exist_tex is not None and _set_compression(exist_tex, "TC_HDR", out,
                                                      "hdr_cube_compress"):
            try:
                EAL.save_asset(dest, only_if_dirty=False)
            except Exception:
                try:
                    EAL.save_asset(dest)
                except Exception:
                    pass
        out[key] = "exists"
        return
    ar = unreal.AssetRegistryHelpers.get_asset_registry()
    try:
        datas = ar.get_assets_by_class(
            unreal.TopLevelAssetPath("/Script/Engine", "TextureCube"), True) or []
    except Exception:
        datas = []
    srcp = None
    for d in datas:
        p = str(d.package_name)
        if p.startswith("/Engine/"):
            srcp = p
            break
    made = False
    if srcp:
        try:
            obj = EAL.duplicate_asset(srcp + "." + srcp.rpartition("/")[2], dest)
            made = obj is not None
        except Exception as e:
            out["cube_dup_err"] = str(e)
    if not made:
        try:
            tools = unreal.AssetToolsHelpers.get_asset_tools()
            made = tools.create_asset("T_CalibCube", FOLDER,
                                      unreal.TextureCube, None) is not None
        except Exception as e:
            out["cube_create_err"] = str(e)
    if made:
        if hdr:
            tex = unreal.load_asset(dest)
            if tex is not None:
                _set_compression(tex, "TC_HDR", out, "hdr_cube_compress")
        try:
            EAL.save_asset(dest, only_if_dirty=False)
        except Exception:
            try:
                EAL.save_asset(dest)
            except Exception:
                pass
    out[key] = {"made": made, "disk": os.path.isfile(disk)}


def _world_name():
    try:
        w = ELL.get_editor_world()
        return str(w.get_name()) if w else ""
    except Exception:
        return ""


def _load_map(dest):
    for call in (lambda: ELL.load_level(dest),
                 lambda: unreal.EditorLoadingAndRenderingUtils.load_map(dest)):
        try:
            if call() is not False:
                return True
        except Exception:
            continue
    return False


def _populate(out):
    mesh = unreal.load_asset(MESH)
    if mesh is None:
        out["populate_err"] = "engine basic shape cube missing: " + MESH
        return
    n = 0
    for i in range(SPAWN_N):
        x = (i % 50) * 260.0
        y = (i // 50) * 260.0
        a = ELL.spawn_actor_from_object(mesh, unreal.Vector(x, y, 100))
        if a is not None:
            n += 1
    out["spawned"] = n
    try:
        ELL.save_current_level()
        out["saved"] = True
    except Exception as e:
        out["saved"] = str(e)


def _actor_mesh(actor):
    """与领域侧 _mesh_probe 同口径的网格读取：拿到的才算数。

    只调 get_static_mesh() 会把真机上带网格的地图自检成 0（5.4 这个方法不在脚本
    绑定里），真值自检必须逐个签名兜底，否则报告本身就是错的。
    """
    comps = []
    for getter in (lambda: actor.get_components_by_class(unreal.StaticMeshComponent),
                   lambda: actor.get_component_by_class(unreal.StaticMeshComponent)):
        try:
            got = getter()
        except Exception:
            continue
        if got is None:
            continue
        comps = list(got) if isinstance(got, (list, tuple)) else [got]
        break
    for comp in comps:
        if comp is None:
            continue
        for getter in (lambda: comp.static_mesh,
                       lambda: comp.get_editor_property("static_mesh"),
                       lambda: comp.get_static_mesh()):
            try:
                mesh = getter()
            except Exception:
                continue
            if mesh is not None:
                return mesh
    return None


def run():
    out = {"actors_before": 0}
    make_cube(out)
    make_cube(out, "T_CalibHdr", hdr=True)
    dest = FOLDER + "/" + MAP
    les = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
    if RESET_MAP and EAL.does_asset_exist(dest):
        try:
            out["deleted_map"] = bool(EAL.delete_asset(dest))
        except Exception as exc:
            out["deleted_map"] = str(exc)
    if not EAL.does_asset_exist(dest):
        opened = False
        try:  # 5.4 真机签名
            opened = bool(les.new_level(dest, False))
        except Exception as e4:
            out["new_level_4arg_err"] = str(e4)
            try:
                opened = bool(les.new_level(dest))
            except Exception as e2:
                out["new_level_err"] = str(e2)
        out["created_map"] = opened
        if not opened:
            raise RuntimeError("new_level failed: %s" % out)
        _populate(out)
    else:
        if _world_name().lower() != MAP.lower():
            out["loaded"] = _load_map(dest)
        actors = ELL.get_all_level_actors() or []
        out["actors_before"] = len(actors)
        if len(actors) < 1000:
            _populate(out)
    actors = ELL.get_all_level_actors() or []
    out["actors_after"] = len(actors)
    # 真值自检：数量够了但网格没押上 = 地图是空壳，后面的合批/HISM
    # 校验会把 1500 个空壳当"散装 Cube"，那是假的消息
    with_mesh = 0
    for actor in actors:
        if _actor_mesh(actor) is not None:
            with_mesh += 1
    out["actors_with_mesh"] = with_mesh
    return out


tmp = _tmp_dir()
os.makedirs(tmp, exist_ok=True)
done = os.path.join(tmp, "done.json")
if os.path.exists(done):
    os.remove(done)
try:
    result = run()
except Exception:
    result = {"error": traceback.format_exc()}
with open(done, "w") as f:
    json.dump(result, f, indent=1)
unreal.log_warning("[PRISM_FIXTURE] level startup done: %s" % (result,))
