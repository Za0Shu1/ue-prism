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


def _ensure_texture(dest_name, png=None):
    """5.3 Interchange 会忽略 destination_name(源文件名决定包名)，故对需要改名的
    基准贴图统一走 duplicate_asset(显式目标名，跨版本稳)。优先复制已导入的 T_Used1K；
    基准缺失时回退到 png 导入(仅当源文件名恰等于 dest_name 时在 5.3 才有效)。"""
    dest = FOLDER + "/" + dest_name
    if EAL.does_asset_exist(dest):
        EAL.delete_asset(dest)
    base = FOLDER + "/T_Used1K"
    if EAL.does_asset_exist(base):
        dup = EAL.duplicate_asset(base, dest)
        if dup is not None and EAL.does_asset_exist(dest):
            EAL.save_asset(dest)
            return True
    if png and os.path.isfile(png):
        return _import_png(png, dest_name)
    return False


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
    def calib_ref():
        """classify_soft 真值靶：新增贴图 T_CalibRef + 材质 M_CalibRef 硬引用它。
        5.4 headless：MaterialEditingLibrary 建材质可用；材质->贴图为硬 package 引用。"""
        png = os.path.join(pngs, "T_Used1K.png")
        if not os.path.isfile(png):
            raise RuntimeError("missing png for T_CalibRef: " + png)
        if not _ensure_texture("T_CalibRef", png=png):
            raise RuntimeError("import T_CalibRef failed")
        dest = FOLDER + "/M_CalibRef"
        if EAL.does_asset_exist(dest):
            EAL.delete_asset(dest)
        at = unreal.AssetToolsHelpers.get_asset_tools()
        mat = at.create_asset("M_CalibRef", FOLDER, unreal.Material,
                              unreal.MaterialFactoryNew())
        if mat is None:
            raise RuntimeError("create M_CalibRef failed")
        tex = unreal.load_asset(FOLDER + "/T_CalibRef")
        expr = unreal.MaterialEditingLibrary.create_material_expression(
            mat, unreal.MaterialExpressionTextureSample, -300, 0)
        expr.set_editor_property("texture", tex)
        unreal.MaterialEditingLibrary.connect_material_property(
            expr, "", unreal.MaterialProperty.MP_BASE_COLOR)
        EAL.save_asset(dest)
        return "M_CalibRef hard-refs T_CalibRef (classify_soft target)"

    step("calib_ref", calib_ref)

    def calib_broken():
        """坏引用真值靶(scan_broken_references)：造材质 M_CalibBroken 硬引用贴图 T_CalibBroken，
        随后删除该贴图(不留桩) -> M_CalibBroken 留一条指向已不存在包的悬空硬依赖(等价 cook 的 Could not find package)。
        5.4 headless 实证: delete_asset 可用; 材质 import 表仍记录已删包路径。"""
        png = os.path.join(pngs, "T_Used1K.png")
        if not os.path.isfile(png):
            raise RuntimeError("missing png for T_CalibBroken: " + png)
        if not _ensure_texture("T_CalibBroken", png=png):
            raise RuntimeError("import T_CalibBroken failed")
        dest = FOLDER + "/M_CalibBroken"
        if EAL.does_asset_exist(dest):
            EAL.delete_asset(dest)
        at = unreal.AssetToolsHelpers.get_asset_tools()
        mat = at.create_asset("M_CalibBroken", FOLDER, unreal.Material,
                              unreal.MaterialFactoryNew())
        if mat is None:
            raise RuntimeError("create M_CalibBroken failed")
        tex = unreal.load_asset(FOLDER + "/T_CalibBroken")
        expr = unreal.MaterialEditingLibrary.create_material_expression(
            mat, unreal.MaterialExpressionTextureSample, -300, 0)
        expr.set_editor_property("texture", tex)
        unreal.MaterialEditingLibrary.connect_material_property(
            expr, "", unreal.MaterialProperty.MP_BASE_COLOR)
        EAL.save_asset(dest)
        tgt = FOLDER + "/T_CalibBroken"
        ok_del = EAL.delete_asset(tgt)
        return {"deleted_texture": bool(ok_del), "texture": tgt,
                "expect": "M_CalibBroken missing hard-dep on " + tgt}

    step("calib_broken", calib_broken)

    def calib_stub():
        """fix_broken_references 真机写靶：M_CalibStub 硬引用 T_CalibStub，随后把贴图 rename_asset
        改名走 -> M_CalibStub 仍 import 旧路径。5.4 python 实证：rename_asset 不生成可持久化的
        ObjectRedirector 桩(旧路径直接消失)，故该靶实为「改名未留桩」型 missing 悬空引用——
        用于验证 fix 的双钥写执行 + 可修复(redirector)/不可修复(missing)路由。
        redirector 自动修复的正样本无头不可靠生成 -> 记 L3(其路由逻辑已由 L0 覆盖)。"""
        png = os.path.join(pngs, "T_Used1K.png")
        if not os.path.isfile(png):
            raise RuntimeError("missing png for T_CalibStub: " + png)
        for nm in ("M_CalibStub", "T_CalibStub", "T_CalibStubMoved"):
            if EAL.does_asset_exist(FOLDER + "/" + nm):
                EAL.delete_asset(FOLDER + "/" + nm)
        if not _ensure_texture("T_CalibStub", png=png):
            raise RuntimeError("import T_CalibStub failed")
        at = unreal.AssetToolsHelpers.get_asset_tools()
        mat = at.create_asset("M_CalibStub", FOLDER, unreal.Material,
                              unreal.MaterialFactoryNew())
        if mat is None:
            raise RuntimeError("create M_CalibStub failed")
        tex = unreal.load_asset(FOLDER + "/T_CalibStub")
        expr = unreal.MaterialEditingLibrary.create_material_expression(
            mat, unreal.MaterialExpressionTextureSample, -300, 0)
        expr.set_editor_property("texture", tex)
        unreal.MaterialEditingLibrary.connect_material_property(
            expr, "", unreal.MaterialProperty.MP_BASE_COLOR)
        EAL.save_asset(FOLDER + "/M_CalibStub")
        rn = EAL.rename_asset(FOLDER + "/T_CalibStub", FOLDER + "/T_CalibStubMoved")
        EAL.save_asset(FOLDER + "/T_CalibStubMoved")
        if EAL.does_asset_exist(FOLDER + "/T_CalibStub"):
            EAL.save_asset(FOLDER + "/T_CalibStub")
        return {"renamed": bool(rn),
                "stub_exists": EAL.does_asset_exist(FOLDER + "/T_CalibStub"),
                "expect": "M_CalibStub -> redirector-backed broken dep /Game/PrismCalib/T_CalibStub"}

    step("calib_stub", calib_stub)

    out["assets"] = sorted(str(d.package_name) for d in
                           (unreal.AssetRegistryHelpers.get_asset_registry()
                            .get_assets_by_path(unreal.Name(FOLDER), True) or []))
    with open(os.path.join(tmp, SUMMARY_NAME), "w") as f:
        json.dump(out, f, indent=1)
    unreal.log_warning("[PRISM_FIXTURE] assets done tmp=%s errors=%d"
                       % (tmp, len(out["errors"])))


main()