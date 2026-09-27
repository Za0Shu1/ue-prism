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
MESH = "/Engine/BasicShapes/Cube.Cube"


def _tmp_dir():
    d = os.environ.get("PRISM_FIXTURE_TMP")
    if not d:
        d = os.path.join(str(unreal.SystemLibrary.get_project_directory()),
                         "Saved", "PrismFixture")
    return d


def make_cube(out):
    """TextureCube 靶：引擎自带立方图复制一份（GUI 下可行；commandlet 会崩）。"""
    dest = FOLDER + "/T_CalibCube"
    disk = os.path.join(str(unreal.SystemLibrary.get_project_directory()),
                        "Content", "PrismCalib", "T_CalibCube.uasset")
    if os.path.isfile(disk) and EAL.does_asset_exist(dest):
        out["cube"] = "exists"
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
        try:
            EAL.save_asset(dest, only_if_dirty=False)
        except Exception:
            try:
                EAL.save_asset(dest)
            except Exception:
                pass
    out["cube"] = {"made": made, "disk": os.path.isfile(disk)}


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


def run():
    out = {"actors_before": 0}
    make_cube(out)
    dest = FOLDER + "/" + MAP
    les = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
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
    out["actors_after"] = len(ELL.get_all_level_actors() or [])
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