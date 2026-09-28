# -*- coding: utf-8 -*-
"""夹具资产生成（commandlet 内跑：-run=pythonscript -script=本文件）。

守 UE 内嵌 Python 3.7 子集；只做资产（import/复制），关卡与 TextureCube
留给 gen_level_startup.py（5.4 实证这两类操作 commandlet 下会崩）。
PNG 目录取环境变量 PRISM_FIXTURE_TMP/pngs，缺省 <工程>/Saved/PrismFixture/pngs。
"""
import json
import os
import traceback

import unreal

EAL = unreal.EditorAssetLibrary
FOLDER = "/Game/PrismCalib"
SUMMARY_NAME = "assets_summary.json"


def _tmp_dir():
    d = os.environ.get("PRISM_FIXTURE_TMP")
    if not d:
        d = os.path.join(str(unreal.SystemLibrary.get_project_directory()),
                         "Saved", "PrismFixture")
    return d


def _import_png(png, name):
    task = unreal.AssetImportTask()
    task.set_editor_property("filename", png)
    task.set_editor_property("destination_path", FOLDER)
    task.set_editor_property("destination_name", name)
    task.set_editor_property("automated", True)
    task.set_editor_property("replace_existing", True)
    task.set_editor_property("save", True)
    unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks([task])
    return EAL.does_asset_exist(FOLDER + "/" + name)


def main():
    tmp = _tmp_dir()
    pngs = os.path.join(tmp, "pngs")
    out = {"folder": FOLDER, "steps": [], "errors": []}

    def step(name, fn):
        try:
            out["steps"].append({"step": name, "ok": True, "detail": fn()})
        except Exception:
            tr = traceback.format_exc()
            unreal.log_warning(tr)
            out["errors"].append({"step": name, "trace": tr})

    step("ensure_folder", lambda: EAL.make_directory(FOLDER)
         if not EAL.does_asset_exist(FOLDER) else True)
    for name in ("T_BigNoise", "T_Used1K", "T_Orphan1", "T_Orphan2", "T_Orphan3"):
        png = os.path.join(pngs, name + ".png")
        if os.path.isfile(png):
            step("import_" + name, lambda png=png, name=name: _import_png(png, name))
        else:
            out["errors"].append({"step": "import_" + name,
                                  "trace": "missing png: " + png})

    def fake_big():
        dest = FOLDER + "/T_FakeBig"
        if EAL.does_asset_exist(dest):
            EAL.delete_asset(dest)
        dup = EAL.duplicate_asset(FOLDER + "/T_BigNoise", dest)
        if dup is None:
            raise RuntimeError("duplicate T_BigNoise failed")
        tex = unreal.load_asset(dest)
        tex.set_editor_property("max_texture_size", 128)
        EAL.save_asset(dest)
        return "T_FakeBig max_texture_size=128"

    step("fake_big", fake_big)

    def calib_mesh():
        dest = FOLDER + "/CalibMesh"
        if EAL.does_asset_exist(dest):
            EAL.delete_asset(dest)
        if EAL.duplicate_asset("/Engine/BasicShapes/Cube", dest) is None:
            raise RuntimeError("duplicate engine cube failed")
        EAL.save_asset(dest)
        return "CalibMesh (engine cube copy; known 1 material slot)"
    step("calib_mesh", calib_mesh)
    out["assets"] = sorted(str(d.package_name) for d in
                           (unreal.AssetRegistryHelpers.get_asset_registry()
                            .get_assets_by_path(unreal.Name(FOLDER), True) or []))
    with open(os.path.join(tmp, SUMMARY_NAME), "w") as f:
        json.dump(out, f, indent=1)
    unreal.log_warning("[PRISM_FIXTURE] assets done tmp=%s errors=%d"
                       % (tmp, len(out["errors"])))


main()