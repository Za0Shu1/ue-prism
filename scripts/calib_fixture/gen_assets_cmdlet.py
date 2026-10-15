# -*- coding: utf-8 -*-
"""夹具资产生成（commandlet 内跑：-run=pythonscript -script=本文件）。

守 UE 内嵌 Python 3.7 子集；只做资产（import/复制），关卡与 TextureCube
留给 gen_level_startup.py（5.4 实证这两类操作 commandlet 下会崩）。
PNG 目录取环境变量 PRISM_FIXTURE_TMP/pngs，缺省 <工程>/Saved/PrismFixture/pngs。
"""
import json
import os
import shutil
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


def _pin_virtual_off(tex):
    """显式关掉贴图的 VT 并回读确认。

    为什么必须钉：UE 5.3 的贴图导入器按 UTextureImportSettings.AutoVTSize
    (源像素数 >= 4096x4096 就 auto VT) 把大贴图置为 VirtualTextureStreaming=true，
    5.4 起该行为取消 -> 不显式关，5.3 上"该开 VT 的 4K 大图"这块真值会被引擎
    预开成 already_virtual，钳尺寸/VT 两条候选全部消失（跨版本矩阵假红）。
    """
    tried = []
    try:
        tex.set_editor_property("virtual_texture_streaming", False)
        tried.append("virtual_texture_streaming:set")
    except Exception as exc:
        tried.append("virtual_texture_streaming:" + type(exc).__name__)
    try:
        off = not bool(tex.get_editor_property("virtual_texture_streaming"))
    except Exception as exc:
        tried.append("readback:" + type(exc).__name__)
        off = False
    return {"off": off, "tried": tried}


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
    # 夹具必须可重复跑：上一轮 migrate_asset 的复制靶留在 _pwtmp（5.4 真机实测：残留
    # 让下一轮 dry-run 报 collision 并拒绝写入，进而拖垮整张兼容矩阵），每轮开头清干净。
    tmp_pkg = FOLDER + "/_pwtmp"
    try:
        if EAL.does_directory_exist(tmp_pkg):
            out["steps"].append("delete_directory=%s" % EAL.delete_directory(tmp_pkg))
    except Exception as exc:
        out["errors"].append("delete_directory: %r" % (exc,))
    disk_tmp = os.path.join(str(unreal.SystemLibrary.get_project_content_directory()),
                            "PrismCalib", "_pwtmp")
    if os.path.isdir(disk_tmp):
        shutil.rmtree(disk_tmp, ignore_errors=True)
        out["steps"].append("rmtree=%s" % disk_tmp.replace("\\", "/"))

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
        vt = _pin_virtual_off(tex)
        EAL.save_asset(dest)
        return {"max_texture_size": 128, "virtual_off": vt,
                "truth": "T_FakeBig = 54MB 源盘/钳到 128 -> disk_only_bloat"}

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


    def calib_unclamped():
        """流送池真值靶(不钳大图)：T_BigNoise 显式 max_texture_size=0(不限) + 关 VT。
        已知真值：source_width=4096、无 MaxSize、非虚拟 -> scan_texture_streaming 必给
        clamp_to=2048 且进 need_clamp；opt_texture_set_virtual 必须把它 planned 而不是
        当成 already_virtual 跳过。与之对照 T_FakeBig(钳到 128)不得出现在 need_clamp。"""
        pkg = FOLDER + "/T_BigNoise"
        tex = unreal.load_asset(pkg)
        if tex is None:
            raise RuntimeError("load T_BigNoise failed")
        tex.set_editor_property("max_texture_size", 0)
        vt = _pin_virtual_off(tex)
        EAL.save_asset(pkg)
        return {"max_texture_size": 0, "virtual_off": vt,
                "truth": "T_BigNoise = 未钳未虚拟 4K -> clamp_to=2048 / VT 可开"}

    step("calib_unclamped", calib_unclamped)

    def calib_icon():
        """禁流送格式真值靶：T_CalibIcon = T_Used1K 副本 + TC_EDITORICON。
        已知真值：format_class='no_stream' -> pool_mb=0 且 permanent_mb>0（整张恒常驻），
        且不得被当成 4B/px 未压缩虚报 3/4 收益。"""
        if not _ensure_texture("T_CalibIcon", png=os.path.join(pngs, "T_Used1K.png")):
            raise RuntimeError("make T_CalibIcon failed")
        tex = unreal.load_asset(FOLDER + "/T_CalibIcon")
        if tex is None:
            raise RuntimeError("load T_CalibIcon failed")
        hit = None
        errs = []
        enum = getattr(unreal, "TextureCompressionSettings", None)
        for member in ("TC_EDITORICON", "TC_EDITOR_ICON", "EDITORICON"):
            value = getattr(enum, member, None) if enum is not None else None
            if value is None:
                errs.append(member + ":no_member")
                continue
            try:
                tex.set_editor_property("compression_settings", value)
            except Exception as exc:
                errs.append(member + ":" + type(exc).__name__)
                continue
            hit = member
            break
        if hit is None:
            raise RuntimeError("compression_settings write failed: %s" % (errs,))
        got = tex.get_editor_property("compression_settings")
        EAL.save_asset(FOLDER + "/T_CalibIcon")
        return {"set": hit, "readback": str(got), "errs": errs}

    step("calib_icon", calib_icon)

    def calib_translucent_material():
        """半透明材质 M_CalibTranslucent（Nanite 冲突真值的材质侧）。"""
        pkg = FOLDER + "/M_CalibTranslucent"
        if EAL.does_asset_exist(pkg):
            EAL.delete_asset(pkg)
        at = unreal.AssetToolsHelpers.get_asset_tools()
        mat = at.create_asset("M_CalibTranslucent", FOLDER, unreal.Material,
                              unreal.MaterialFactoryNew())
        if mat is None:
            raise RuntimeError("create M_CalibTranslucent failed")
        hit = None
        errs = []
        bm = getattr(unreal, "BlendMode", None)
        for member in ("BLEND_TRANSLUCENT", "BLEND_Translucent", "TRANSLUCENT"):
            value = getattr(bm, member, None) if bm is not None else None
            if value is None:
                errs.append(member + ":no_member")
                continue
            try:
                mat.set_editor_property("blend_mode", value)
            except Exception as exc:
                errs.append(member + ":" + type(exc).__name__)
                continue
            hit = member
            break
        if hit is None:
            raise RuntimeError("blend_mode write failed: %s" % (errs,))
        got = mat.get_editor_property("blend_mode")
        EAL.save_asset(pkg)
        return {"set": hit, "readback": str(got), "errs": errs}

    step("calib_translucent_material", calib_translucent_material)

    def _slot_materials(mesh):
        """当前槽位材质（回读口径）。5.4 实证 static_materials[i].material_interface，
        该属性取不到时退到 get_material(section_index)。"""
        out = []
        try:
            for slot in (mesh.get_editor_property("static_materials") or []):
                try:
                    got = slot.get_editor_property("material_interface")
                except Exception:
                    got = None
                if got is not None:
                    out.append(got)
        except Exception:
            pass
        if out:
            return out
        try:
            for i in range(int(mesh.get_num_sections(0) or 0)):
                got = mesh.get_material(i)
                if got is not None:
                    out.append(got)
        except Exception:
            pass
        return out

    def _assign_material(mesh, mat):
        """把网格槽位材质换成半透明材质。多候选(跨版本签名漂移) + 回读证据。"""
        # 5.4 headless 实测：get_lod_used_materials/get_used_materials 不存在、槽位元素
        # 字段是 material_interface（写 .material 抛异常）、另有直写访问器
        # set_material(index, material)。旧实现三条候选全落空却静默返回，靶材质仍是引擎
        # 不透明材质，nanite.* 5 项校验整组红——造靶失败必须当场响，不留给下游猜。
        tried = []
        want = str(mat.get_path_name()).split(".")[0]

        def back():
            return [str(m.get_path_name()).split(".")[0] for m in _slot_materials(mesh)]

        before = back()
        lib = getattr(unreal, "StaticMeshEditorLibrary", None)
        if lib is not None:
            for old in _slot_materials(mesh):
                if str(old.get_path_name()).split(".")[0] == want:
                    continue
                try:
                    lib.replace_material(mesh, old, mat)
                    tried.append({"sig": "StaticMeshEditorLibrary.replace_material(old->new)"})
                except Exception as exc:
                    tried.append({"sig": "StaticMeshEditorLibrary.replace_material",
                                  "err": type(exc).__name__})
                    break
                if want in back():
                    return {"readback_materials": back(), "hit": True,
                            "tried": tried, "before": before}
        try:
            sections = int(mesh.get_num_sections(0) or 0)
        except Exception as exc:
            sections = 0
            tried.append({"sig": "get_num_sections(0)", "err": type(exc).__name__})
        if sections and hasattr(mesh, "set_material"):
            done = True
            for i in range(sections):
                try:
                    mesh.set_material(i, mat)
                except Exception as exc:
                    done = False
                    tried.append({"sig": "set_material(%d)" % i,
                                  "err": type(exc).__name__})
                    break
            if done:
                tried.append({"sig": "set_material(i, mat)", "sections": sections})
            if want in back():
                return {"readback_materials": back(), "hit": True,
                        "tried": tried, "before": before}
        try:
            slots = list(mesh.get_editor_property("static_materials") or [])
            for slot in slots:
                for key in ("material_interface", "material"):
                    try:
                        slot.set_editor_property(key, mat)
                        break
                    except Exception:
                        continue
            if slots:
                mesh.set_editor_property("static_materials", slots)
                tried.append({"sig": "static_materials[i].material_interface",
                              "slots": len(slots)})
        except Exception as exc:
            tried.append({"sig": "static_materials[i].material_interface",
                          "err": type(exc).__name__})
        used = []
        try:
            used = list(mesh.get_lod_used_materials(0) or [])
        except Exception as exc:
            tried.append({"sig": "get_lod_used_materials(0)", "err": type(exc).__name__})
        got = back()
        return {"readback_materials": got, "hit": want in got, "tried": tried,
                "before": before,
                "lod_used": [str(m.get_path_name()).split(".")[0] for m in used]}

    def calib_nanite_target():
        """Nanite+半透明冲突真值靶：CalibMeshTranslucent = 引擎 Cube 副本 + 半透明材质。
        故意不开 Nanite：检查脚本先用 opt_mesh_set_nanite(skip_translucent=False) 强行开
        (验拦截与真写)，再用 fix_nanite_translucent 关回(自还原成 blocked_translucent)。"""
        pkg = FOLDER + "/CalibMeshTranslucent"
        if EAL.does_asset_exist(pkg):
            EAL.delete_asset(pkg)
        if EAL.duplicate_asset("/Engine/BasicShapes/Cube", pkg) is None:
            raise RuntimeError("duplicate engine cube failed")
        mesh = unreal.load_asset(pkg)
        if mesh is None:
            raise RuntimeError("load CalibMeshTranslucent failed")
        mat = unreal.load_asset(FOLDER + "/M_CalibTranslucent")
        if mat is None:
            raise RuntimeError("load M_CalibTranslucent failed (dependency)")
        info = _assign_material(mesh, mat)
        EAL.save_asset(pkg)
        if not info.get("hit"):
            raise RuntimeError("半透明靶没造上：CalibMeshTranslucent 材质替换失败 %s"
                               % (info.get("tried"),))
        return info

    step("calib_nanite_target", calib_nanite_target)

    def calib_material_instance():
        """材质实例真值靶：M_CalibInst(MaterialInstanceConstant，父=M_CalibRef) 挂上
        CalibMeshInstance。5.4 探针实证：MIC 直读 used_with_nanite 必抛异常，
        该开关只在父 UMaterial 上（get_base_material() 才够得着）——真实工程某网格
        5/5 材质因此恒 null。已知真值 = scan 必须给出非 null，且 usage_api 经基类材质。
        """
        mic_pkg = FOLDER + "/M_CalibInst"
        mesh_pkg = FOLDER + "/CalibMeshInstance"
        for nm in (mic_pkg, mesh_pkg):
            if EAL.does_asset_exist(nm):
                EAL.delete_asset(nm)
        base = unreal.load_asset(FOLDER + "/M_CalibRef")
        if base is None:
            raise RuntimeError("load M_CalibRef failed (instance target parent)")
        at = unreal.AssetToolsHelpers.get_asset_tools()
        errs = []
        mic = None
        for fac in ("MaterialInstanceConstantFactoryNew", "ObjectFactoryNew"):
            maker = getattr(unreal, fac, None)
            if maker is None:
                errs.append(fac + ":no_class")
                continue
            try:
                mic = at.create_asset("M_CalibInst", FOLDER,
                                      unreal.MaterialInstanceConstant, maker())
            except Exception as exc:
                errs.append(fac + ":" + type(exc).__name__)
                continue
            if mic is not None:
                errs.append(fac + ":hit")
                break
        if mic is None:
            # 兜底：复制引擎自带 MIC（同为 MaterialInstanceConstant，不透明）
            try:
                mic = EAL.duplicate_asset(
                    "/Engine/EngineMaterials/Widget3DPassThrough_Opaque", mic_pkg)
                errs.append("duplicate_engine_mic")
            except Exception as exc:
                errs.append("duplicate:" + type(exc).__name__)
        if mic is None:
            raise RuntimeError("create MaterialInstanceConstant failed: %s" % (errs,))
        set_log = []
        for key in ("parent", "base_material"):
            try:
                mic.set_editor_property(key, base)
                set_log.append(key + ":ok")
                break
            except Exception as exc:
                set_log.append(key + ":" + type(exc).__name__)
        try:
            parent_back = str(mic.get_editor_property("parent"))
        except Exception as exc:
            parent_back = "ERR:" + type(exc).__name__
        EAL.save_asset(mic_pkg)
        if EAL.duplicate_asset("/Engine/BasicShapes/Cube", mesh_pkg) is None:
            raise RuntimeError("duplicate engine cube failed (instance mesh)")
        mesh = unreal.load_asset(mesh_pkg)
        if mesh is None:
            raise RuntimeError("load CalibMeshInstance failed")
        info = _assign_material(mesh, unreal.load_asset(mic_pkg) or mic)
        EAL.save_asset(mesh_pkg)
        if not info.get("hit"):
            raise RuntimeError("材质实例没挂上 CalibMeshInstance: %s" % (info.get("tried"),))
        return {"mic": mic_pkg, "create": errs, "parent_set": set_log,
                "parent_readback": parent_back, "slots": info.get("readback_materials")}

    step("calib_material_instance", calib_material_instance)

    out["assets"] = sorted(str(d.package_name) for d in
                           (unreal.AssetRegistryHelpers.get_asset_registry()
                            .get_assets_by_path(unreal.Name(FOLDER), True) or []))
    with open(os.path.join(tmp, SUMMARY_NAME), "w") as f:
        json.dump(out, f, indent=1)
    unreal.log_warning("[PRISM_FIXTURE] assets done tmp=%s errors=%d"
                       % (tmp, len(out["errors"])))


main()
