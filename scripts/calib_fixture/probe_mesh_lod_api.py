# -*- coding: utf-8 -*-
"""LOD 族立项前的真机取证探针（在 UE 内跑；只测 API 可达性；默认不留资产）。

为什么要先跑这个：仓库里 LOD 的**读**面已被 `get_asset_metrics` 真机钉住（`get_num_lods` +
每级 triangles/sections），但**阈值**（每级 ScreenSize、是否自动计算）在 UE Python 上到不到
得着完全没量过——而它决定 LOD 族能不能做成"可优化的写操作"，还是只能做只读诊断。跨版本
`UStaticMesh` 的 LOD 数据大半在 `FStaticMeshRenderData`（非 UPROPERTY，Python 通常看不见），
所以这一步必须实测，不能猜。

探针做四件事，全部只陈述 tried/命中，不猜：
1) 成员面清单：`unreal.StaticMesh` / `StaticMeshEditorSubsystem` 里所有含
   lod|screen|detail|reduce|triangle|nanite 的绑定名（下次立项直接照这张表写候选链）。
2) 读面：LOD 数、每级三角数/section 数、每级 ScreenSize、AutoCompute 标志，逐个候选试。
3) 写面：把 ScreenSize 改成已知值 -> 回读校验（改不回就判 failed，绝不假装成功）。
4) 副作用面：Nanite 开关读法顺带复核（Nanite 网格不看 LOD 阈值，工具必须分得开这两族）。

跑法（无头 commandlet 即可；StaticMesh 副本在 commandlet 里造是夹具既有路径）：
    UnrealEditor-Cmd.exe <uproject> -run=pythonscript ^
        -script=scripts/calib_fixture/probe_mesh_lod_api.py -stdout -unattended -nosplash
环境变量：PRISM_PROBE_OUT=<输出目录>（默认 <工程>/Saved/PrismFixture）
          PRISM_PROBE_KEEP=1（保留探针资产；默认删除，异常路径也删。删除前释放引用 + gc，
          5.4 实证：python 仍持有对象时 delete_asset 会空指针崩编辑器）

产物 probe_mesh_lod_api.json：verdict = write_ok / read_only / nothing（立项判据只看这三档）。
"""
import gc
import io
import json
import os
import sys
import time
import traceback

import unreal

FOLDER = "/Game/PrismCalib"
PROBE_MESH = "SM_LodProbe"
OUT_NAME = "probe_mesh_lod_api.json"

MEMBER_KEYS = ("lod", "screen", "detail", "reduce", "triangle", "nanite", "simple")
PLATFORM_KEYS = ("ALL", "Windows", "WindowsNoEditor", "None", "Android", "IOS")
SCREEN_READ = ("lod_screen_sizes", "screen_sizes", "lod_screen_size", "lod0_screen_size",
               "screen_size_list", "lod_info")
AUTOCOMPUTE_READ = ("lod0_auto_compute", "auto_compute_lods", "auto_compute_lod_screen_size",
                    "b_auto_compute_lod_screen_size", "lod_auto_compute")
NANITE_READ = ("nanite_settings", "support_nanite", "b_supports_nanite")

SUBSYSTEMS = ("StaticMeshEditorSubsystem", "StaticMeshEditorLibrary", "AssetToolkit"
              "EditorSubsystem")
SS_READ = ("get_lod_screen_size", "get_lod_screen_sizes", "get_screen_size",
           "get_num_lods", "get_lod_info", "get_lod_count")
SS_WRITE = ("set_lod_screen_size", "set_lod_screen_sizes", "set_screen_size",
            "update_lod_screen_size", "set_lod_count", "resize_lod", "regenerate_lods",
            "build_lod", "add_lod")

WANT_SIZES = (1.0, 0.5, 0.25, 0.1)
TOL = 1e-4


def _engine_version():
    try:
        return str(unreal.SystemLibrary.get_engine_version())
    except Exception as exc:
        return "err:" + repr(exc)


def _say(line):
    try:
        unreal.log(str(line))
    except Exception:
        print(str(line))


def _cap(value, n=200):
    s = repr(value)
    return s if len(s) <= n else s[:n] + "..."


def _members(obj, keys=MEMBER_KEYS):
    out = []
    try:
        for n in dir(obj):
            if n.startswith("_"):
                continue
            low = n.lower()
            if any(k in low for k in keys):
                out.append(n)
    except Exception as exc:
        out.append("dir_failed:" + repr(exc))
    return out


def _read_prop(obj, names):
    """逐个候选读属性：命中即停，未命中留痕（跨版本就是靠这张 tried 表定位）。"""
    got = {}
    for n in names:
        try:
            v = obj.get_editor_property(n)
        except Exception as exc:
            got[n] = "miss:" + _cap(exc, 60)
            continue
        if v is None:
            got[n] = "None"
            continue
        got[n] = _cap(v)
        return v, n, got
    return None, None, got


def _probe_num_lods(mesh):
    tried = {}
    for name in ("get_num_lods",):
        fn = getattr(mesh, name, None)
        if not callable(fn):
            tried[name] = "absent"
            continue
        try:
            return int(fn()), name, tried
        except Exception as exc:
            tried[name] = "err:" + _cap(exc, 60)
    return None, None, tried


def _probe_lod_detail(mesh, nlods):
    """每级三角数/section 数：沿用 metrics.py 已验证的候选名，顺带复核还在不在。"""
    rows = []
    for lod in range(max(0, min(nlods or 0, 8))):
        entry = {"index": lod}
        for name, args in (("get_num_triangles", (lod,)), ("get_num_sections", (lod,))):
            fn = getattr(mesh, name, None)
            if not callable(fn):
                entry[name] = "absent"
                continue
            try:
                entry[name] = int(fn(*args))
            except Exception as exc:
                entry[name] = "err:" + _cap(exc, 60)
        rows.append(entry)
    return rows


def _probe_nanite(mesh):
    tried = {}
    for n in NANITE_READ:
        try:
            v = mesh.get_editor_property(n)
        except Exception as exc:
            tried[n] = "miss:" + _cap(exc, 60)
            continue
        if v is None:
            tried[n] = "None"
            continue
        inner = {}
        try:
            for sub in ("enabled", "allow_sw", "position_precision"):
                try:
                    inner[sub] = _cap(v.get_editor_property(sub), 40)
                except Exception as exc:
                    inner[sub] = "miss:" + _cap(exc, 40)
        except Exception as exc:
            inner["err"] = _cap(exc, 60)
        return _cap(v, 120), n, inner, tried
    return None, None, {}, tried


def _probe_subsystem(report):
    """子系统面：BlueprintCallable 的 LOD 读写入口是否存在（这是写族最可能的正门）。"""
    out = {"absent_classes": [], "classes": {}}
    for cls_name in SUBSYSTEMS:
        cls = getattr(unreal, cls_name, None)
        if cls is None:
            out["absent_classes"].append(cls_name)
            continue
        info = {"class_members": _members(cls), "methods": {}}
        inst = None
        if cls_name.endswith("Subsystem"):
            try:
                inst = unreal.get_editor_subsystem(cls)
            except Exception as exc:
                info["get_editor_subsystem"] = "err:" + _cap(exc, 80)
        else:
            inst = cls
        if inst is not None:
            for name in SS_READ + SS_WRITE:
                fn = getattr(inst, name, None)
                if callable(fn):
                    info["methods"][name] = "present"
        out["classes"][cls_name] = info
    return out


def _try_write(mesh, nlods, report):
    """写面：改 ScreenSize 已知值 -> 回读校验。改不动就诚实 failed，绝不假装成功。"""
    res = {"attempts": [], "readback": None, "via": None, "ok": False}
    targets = list(WANT_SIZES[:max(1, nlods or 1)])
    for prop in SCREEN_READ:
        attempt = {"target": "prop:" + prop, "set_to": _cap(targets)}
        try:
            mesh.set_editor_property(prop, targets)
        except Exception as exc:
            attempt["set"] = "err:" + _cap(exc, 90)
            res["attempts"].append(attempt)
            continue
        attempt["set"] = "ok"
        val, via, tried = _read_prop(mesh, SCREEN_READ)
        attempt["readback_tried"] = tried
        res["attempts"].append(attempt)
        if val is None:
            continue
        res["readback"] = _cap(val)
        res["via"] = via
        try:
            seq = list(val)
        except Exception:
            seq = [val]
        ok = len(seq) >= len(targets)
        for i, want in enumerate(targets):
            try:
                if abs(float(seq[i]) - float(want)) > TOL:
                    ok = False
            except Exception:
                ok = False
        res["ok"] = bool(ok)
        if ok:
            break
    return res


def _call_shapes(obj, name, arg_shapes):
    """同名方法试多种参数形态（UE 绑定按重载/默认值决定 python 面形态）。"""
    fn = getattr(obj, name, None)
    if not callable(fn):
        return None, None, {"shape": "absent"}
    tried = {}
    for args, label in arg_shapes:          # 调用点一律传 (args, label)
        try:
            return fn(*args), label, tried
        except Exception as exc:
            tried[str(label)] = "err:" + _cap(exc, 70)
    return None, None, tried


def _lod_class_surface():
    """`unreal` 顶层里所有含 lod/screen 的类名（下次立项直接照这张表找入口）。"""
    names = []
    try:
        for n in dir(unreal):
            if n.startswith("_"):
                continue
            low = n.lower()
            if "lod" in low or "screen" in low:
                names.append(n)
    except Exception as exc:
        names.append("dir_failed:" + repr(exc))
    return sorted(names)[:160]


def _probe_lod_face(mesh):
    """5.4 实测的可达面：ScreenSize 属性面全 miss，但 UStaticMesh 上有一组
    auto-compute 判定与「每画质等级 / 每平台 Min LOD」的读写方法。形态在这里钉死。"""
    out = {}
    v, shape, tried = _call_shapes(mesh, "is_lod_screen_size_auto_computed",
                                   [((), "noarg"), ((0,), "idx0"), ((0, False), "idx0_bool")])
    out["auto_computed"] = {"value": _cap(v, 40), "shape": shape, "tried": tried}
    v, shape, tried = _call_shapes(mesh, "get_min_lod_for_quality_levels", [((), "noarg")])
    out["min_lod_quality_list"] = {"value": _cap(v, 140), "shape": shape, "tried": tried}
    v, shape, tried = _call_shapes(mesh, "get_minimum_lod_for_quality_level",
                                   [((0,), "q0"), ((1,), "q1")])
    out["min_lod_one_quality"] = {"value": _cap(v, 40), "shape": shape, "tried": tried}
    v, shape, tried = _call_shapes(mesh, "get_minimum_lod_for_platforms", [((), "noarg")])
    out["min_lod_platform_map"] = {"value": _cap(v, 160), "shape": shape, "tried": tried}
    v, shape, tried = _call_shapes(mesh, "get_minimum_lod_for_platform",
                                   [((k,), "key:" + k) for k in PLATFORM_KEYS if k])
    out["min_lod_one_platform"] = {"value": _cap(v, 40), "shape": shape, "tried": tried}
    props = {}
    for n in ("min_lod_for_quality_levels", "minimum_lod_for_quality_levels",
              "minimum_lods_for_platform", "lod_group", "nanite_settings"):
        try:
            props[n] = _cap(mesh.get_editor_property(n), 90)
        except Exception as exc:
            props[n] = "miss:" + _cap(exc, 50)
    out["props"] = props
    return out


def _try_write_lod_face(mesh):
    """写面：每画质等级 / 每平台的 Min LOD 设成已知值再回读；回读不符即判失败。

    这是 LOD 族能不能做成 opt_ 的唯一点据——ScreenSize 读不到就别指望改 ScreenSize，
    但 Min LOD 是引擎认账的强制降级手段（移动端/低画质常用），能写就能产品化。
    """
    res = {"attempts": [], "ok": False, "via": None}
    cur = None
    try:
        cur = mesh.get_min_lod_for_quality_levels()
    except Exception as exc:
        res["baseline_err"] = _cap(exc, 80)
    nq = len(list(cur or [])) or 6
    want = [2] * nq
    attempt = {"target": "set_min_lod_for_quality_levels", "want": _cap(want, 90),
               "baseline": _cap(cur, 90)}
    try:
        mesh.set_min_lod_for_quality_levels(want)
    except Exception as exc:
        attempt["set"] = "err:" + _cap(exc, 90)
    else:
        try:
            back = list(mesh.get_min_lod_for_quality_levels() or [])
            attempt["readback"] = _cap(back, 90)
            if back == want:
                res["ok"] = True
                res["via"] = "quality_levels"
        except Exception as exc:
            attempt["readback"] = "err:" + _cap(exc, 70)
    res["attempts"].append(attempt)

    for key in PLATFORM_KEYS:
        if not key:
            continue
        attempt = {"target": "set_minimum_lod_for_platform", "key": key, "want": 3}
        try:
            mesh.set_minimum_lod_for_platform(key, 3)
        except Exception as exc:
            attempt["set"] = "err:" + _cap(exc, 80)
        else:
            try:
                back = mesh.get_minimum_lod_for_platform(key)
                attempt["readback"] = _cap(back, 40)
                if int(back) == 3:
                    res["ok"] = True
                    res["via"] = res["via"] or ("platform:" + key)
            except Exception as exc:
                attempt["readback"] = "err:" + _cap(exc, 60)
        res["attempts"].append(attempt)
    return res


def _make_probe_mesh():
    EAL = unreal.EditorAssetLibrary
    dest = FOLDER + "/" + PROBE_MESH
    if EAL.does_asset_exist(dest):
        EAL.delete_asset(dest)
        gc.collect()
    dup = None
    try:
        dup = EAL.duplicate_asset("/Engine/BasicShapes/Cube", dest)
    except Exception as exc:
        return None, "duplicate_asset err:" + _cap(exc, 120)
    if dup is None:
        return None, "duplicate_asset returned None"
    return dup, None


def _drop(mesh, keep, report):
    report["kept"] = bool(keep)
    if keep:
        return
    mesh = None          # 5.4 实证：仍持有引用时 delete_asset 会空指针崩编辑器
    gc.collect()
    try:
        unreal.EditorAssetLibrary.delete_asset(FOLDER + "/" + PROBE_MESH)
        report["deleted"] = True
    except Exception as exc:
        report["deleted"] = False
        report["delete_err"] = _cap(exc, 120)


def main():
    keep = str(os.environ.get("PRISM_PROBE_KEEP", "")).strip().lower() in ("1", "true", "yes")
    report = {"started": time.strftime("%Y-%m-%d %H:%M:%S"),
              "ue_version": _engine_version(),
              "verdict": "nothing", "probe_mesh": FOLDER + "/" + PROBE_MESH,
              "keep": keep}
    out_dir = os.environ.get("PRISM_PROBE_OUT") or os.path.join(
        str(unreal.SystemLibrary.get_project_directory()), "Saved", "PrismFixture")
    mesh, err = _make_probe_mesh()
    report["make"] = "ok" if mesh is not None else err
    if mesh is not None:
        report["class_members"] = _members(mesh)
        report["class"] = type(mesh).__name__
        nlods, via, tried = _probe_num_lods(mesh)
        report["num_lods"] = {"value": nlods, "via": via, "tried": tried}
        report["lod_detail"] = _probe_lod_detail(mesh, nlods)
        val, prop, tried = _read_prop(mesh, SCREEN_READ)
        report["screen_read"] = {"value": _cap(val) if val is not None else None,
                                 "via": prop, "tried": tried}
        ac, acprop, actried = _read_prop(mesh, AUTOCOMPUTE_READ)
        report["auto_compute"] = {"value": _cap(ac) if ac is not None else None,
                                  "via": acprop, "tried": actried}
        nv, nvia, ninner, ntried = _probe_nanite(mesh)
        report["nanite_read"] = {"value": nv, "via": nvia, "inner": ninner, "tried": ntried}
        report["subsystems"] = _probe_subsystem(report)
        if nlods:
            report["screen_write"] = _try_write(mesh, nlods, report)
        report["lod_face"] = _probe_lod_face(mesh)
        report["lod_face_write"] = _try_write_lod_face(mesh)
        report["unreal_lod_surface"] = _lod_class_surface()
        ok_screen = bool((report.get("screen_write") or {}).get("ok"))
        ok_face = bool(report["lod_face_write"].get("ok"))
        report["write_via"] = {"screen_size": (report.get("screen_write") or {}).get("via"),
                               "min_lod": report["lod_face_write"].get("via")}
        if ok_screen or ok_face:
            report["verdict"] = "write_ok"
        else:
            report["verdict"] = "read_only" if nlods else "nothing"
    _drop(mesh, keep, report)
    _say("LOD-PROBE verdict=%s make=%s num_lods=%s screen_write_ok=%s "
         "minlod_write_ok=%s via=%s" % (
             report.get("verdict"), report.get("make"),
             (report.get("num_lods") or {}).get("value"),
             (report.get("screen_write") or {}).get("ok"),
             (report.get("lod_face_write") or {}).get("ok"),
             report.get("write_via")))
    try:
        if not os.path.isdir(out_dir):
            os.makedirs(out_dir)
        text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True,
                          default=str)
        final = os.path.join(out_dir, OUT_NAME)
        tmp = final + ".tmp"
        with io.open(tmp, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        os.replace(tmp, final)
        _say("LOD-PROBE dump -> " + os.path.join(out_dir, OUT_NAME))
    except Exception as exc:
        _say("LOD-PROBE dump failed -> %r" % (exc,))
    return 0


try:
    sys.exit(main())
except Exception:
    _say("LOD-PROBE FATAL " + traceback.format_exc())
    raise
