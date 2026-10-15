# -*- coding: utf-8 -*-
"""#11 B 冷态反推真值探针（在 UE 内跑；只读度量 + 自清理；不进 59 项夹具门禁）。

干什么：把 make_dds.py 造的**已知边长 / 每像素字节数(BPT) / 面数**的 DDS 源导进当前工程，
走域层真管线 (get_asset_metrics + streaming.texture_pool_entry) 读回，逐项对比
"authored 真值 vs 引擎 source_memory vs 反推边长"，量清冷态反推的歧义面：
哪一类会被安全阀挡住（撤金额保发现），哪一类会"伪精确"（exact=true 但边长错、照常报价）。

为什么是独立脚本而不是并进 checks.py：这些靶是**未被引用的散装资产**，直接进夹具目录要把
孤儿/引用类校验的真值集一并改掉；而本探针的产物是"跨版本证据表"（5.3.2 与 5.4.4 实测数字
逐字节一致），不是回归门禁。#11 口径(B-先验 / 面数语义 / C 直读源图)定了之后，
再把选定的靶与断言并进夹具（测试随功能走）。

跑法（任意引擎版本，命令行）：
  UnrealEditor-Cmd.exe <uproject> -run=pythonscript \\
      -script=scripts/calib_fixture/probe_source_truth.py -stdout -unattended -nosplash
环境变量：PRISM_PROBE_DDS=<源图目录>（默认 <工程>/Saved/PrismFixture/dds，缺文件自动生成）
          PRISM_PROBE_OUT=<输出目录>（默认与源图目录同处）
          PRISM_PROBE_KEEP=1（测完保留导入的资产；默认删除，异常退出也会删）
产出：probe_source_truth.json + 引擎日志里的 PROBE 表（commandlet 下 print 不入日志，
所以日志一律走 unreal.log）。另附每个靶的类级成员面枚举（api_surface），用于复核
「slice 数在 Python 面上根本不可读」这条负结论——它是 faces 口径不能单改的依据。
"""
import json
import os
import sys
import traceback

import unreal

FOLDER = "/Game/PrismCalib"
OUT_NAME = "probe_source_truth.json"


def _say(line):
    try:
        unreal.log(str(line))
    except Exception:
        print(str(line))


def _here():
    try:
        return os.path.dirname(os.path.abspath(__file__))
    except Exception:
        return str(unreal.SystemLibrary.get_project_directory())


def _dds_dir():
    cand = os.environ.get("PRISM_PROBE_DDS")
    if cand:
        return cand
    base = os.environ.get("PRISM_FIXTURE_TMP") or os.path.join(
        str(unreal.SystemLibrary.get_project_directory()), "Saved", "PrismFixture")
    return os.path.join(base, "dds")


def _ensure_dds(dds_dir):
    here = _here()
    if here not in sys.path:
        sys.path.insert(0, here)
    from make_dds import make_dds
    return make_dds(dds_dir), dds_dir


def _load_prism():
    """优先用插件里的 domain 拷贝（与真机跑的代码同源），退回仓库目录。"""
    cands = [os.path.join(str(unreal.SystemLibrary.get_project_directory()),
                          "Plugins", "UEPrism", "Content", "Python"), _here()]
    for cand in cands:
        if cand and os.path.isdir(os.path.join(cand, "prism")):
            if cand not in sys.path:
                sys.path.insert(0, cand)
            from prism.domain import metrics as dm
            from prism import streaming as st
            return dm, st, cand
    raise RuntimeError("找不到 prism 包（插件未装或仓库不在 sys.path）")


def _import_target(path, name):
    task = unreal.AssetImportTask()
    task.set_editor_property("filename", path)
    task.set_editor_property("destination_path", FOLDER)
    task.set_editor_property("destination_name", name)
    task.set_editor_property("automated", True)
    task.set_editor_property("replace_existing", True)
    task.set_editor_property("save", True)
    note = ""
    try:
        unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks([task])
    except Exception as exc:
        note = "import_exception: %r" % (exc,)
    try:
        return bool(unreal.EditorAssetLibrary.does_asset_exist(FOLDER + "/" + name)), note
    except Exception as exc:
        return False, (note + " does_asset_exist:%r" % (exc,))


def _api_surface(pkg):
    """类级成员枚举：slice / 面尺寸到底在不在引擎暴露的 Python 面上。

    DDS 立方图实测：导入后类是 Texture2DArray，5.3.2 69 个成员 / 5.4.4 78 个，
    无 array/slice/dim 成员，尺寸成员也没有 blueprint_get_size_x/y（只有
    Texture2D 暴露）-> 面数(array_size)在域层无从直读，faces 口径不能单改。
    原生 TextureCube 类的读数属 B2 待复测问题，本探针不覆盖、不下负结论。
    """
    try:
        obj = unreal.load_asset(pkg)
        if obj is None:
            return {"note": "load_failed"}
        names = [n for n in dir(type(obj)) if not n.startswith("_")]
        low = [n.lower() for n in names]
        return {
            "pytype": type(obj).__name__,
            "members": len(names),
            "array_members": [names[i] for i, n in enumerate(low)
                               if any(k in n for k in ("array", "slice", "dim"))],
            "size_members": [names[i] for i, n in enumerate(low)
                             if "size" in n and "mip" not in n],
        }
    except Exception as exc:
        return {"note": "err:%r" % (exc,)}


def _dump(res, out_path):
    try:
        d = os.path.dirname(out_path)
        if d and not os.path.isdir(d):
            os.makedirs(d)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(res, f, indent=2, sort_keys=True)
    except Exception as exc:
        _say("PROBE dump failed %s -> %r" % (out_path, exc))


def _measure_row(st, row, it):
    m = it.get("metrics") or {}
    src_mem = m.get("source_memory_bytes")
    faces_now = st.faces_for(it.get("class"), m.get("array_size"))
    auth_side = row["authored_width"]
    entry = None
    if m:
        entry = st.texture_pool_entry(m, cls_name=it.get("class"),
                                      disk_bytes=it.get("disk_bytes"))
    derived_side = m.get("source_width")
    # 本次 update 就要用到的派生真值，先算成局部变量（不能自读自写）
    expect_mem = auth_side * auth_side * row["authored_bpt"] * row["authored_faces"]
    row.update({
        "class": it.get("class"), "class_note": it.get("note"), "tried": m.get("tried"),
        "expect_source_memory": expect_mem,
        "source_memory_bytes": src_mem, "source_disk_bytes": m.get("source_disk_bytes"),
        "compression": str(m.get("compression")), "direct_width": m.get("width"),
        "array_size": m.get("array_size"), "faces_used_by_product": faces_now,
        "derived_side": derived_side, "derived_bpt": m.get("source_bytes_per_texel"),
        "derived_exact": m.get("source_side_exact"),
        "size_basis": (entry or {}).get("size_basis"),
        "pool_mb": (entry or {}).get("pool_mb"),
        "clamp_savings_mb": (entry or {}).get("clamp_savings_mb"),
        "vt_savings_mb": (entry or {}).get("vt_savings_mb"),
        "flags": (entry or {}).get("flags"),
        "side_match": None if derived_side is None else (derived_side == auth_side),
        "pseudo_exact": bool(m.get("source_side_exact")) and derived_side not in (None, auth_side),
        "valve_fired": (entry or {}).get("size_basis") == "source_unverified",
        "srcmem_match": None if not src_mem else (src_mem == expect_mem),
    })
    return row


def _report(res):
    hdr = "%-15s %-14s %-8s %-8s %-7s %-12s %-6s %-18s %-6s" % (
        "target", "class", "authored", "srcmem", "faces", "derived", "exact", "size_basis", "match")
    _say(hdr)
    _say("-" * len(hdr))
    for r in res["targets"]:
        _say("%-15s %-14s %-8s %-8s %-7s %-12s %-6s %-18s %-6s" % (
            r["name"], str(r.get("class"))[:14],
            "%d@%dx%d" % (r["authored_width"], r["authored_bpt"], r["authored_faces"]),
            "ok" if r.get("srcmem_match") else str(r.get("srcmem_match")),
            "%s/%s" % (r.get("faces_used_by_product"), r["authored_faces"]),
            "%s@%s" % (r.get("derived_side"), r.get("derived_bpt")),
            str(r.get("derived_exact")), str(r.get("size_basis")), str(r.get("side_match"))))
    for r in res["targets"]:
        api = r.get("api_surface") or {}
        _say("  API %-15s pytype=%-14s members=%s array=%s size=%s" % (
            r["name"], api.get("pytype"), api.get("members"),
            api.get("array_members") or "none", api.get("size_members") or "none"))
    done = [r for r in res["targets"] if r.get("derived_side") is not None]
    _say("SUMMARY engine=%s targets=%d measured=%d srcmem_exact=%d side_match=%d "
         "pseudo_exact=%d valve_fired=%d" % (
             res["engine"], len(res["targets"]), len(done),
             sum(1 for r in done if r.get("srcmem_match")),
             sum(1 for r in done if r.get("side_match")),
             sum(1 for r in done if r.get("pseudo_exact")),
             sum(1 for r in done if r.get("valve_fired"))))


def _main():
    dds_dir = _dds_dir()
    truth, dds_dir = _ensure_dds(dds_dir)
    dm, st, prism_src = _load_prism()
    out_dir = os.environ.get("PRISM_PROBE_OUT") or dds_dir
    out_path = os.path.join(out_dir, OUT_NAME)
    res = {"engine": None, "pool_model": st.POOL_MODEL_VERSION, "prism_src": prism_src,
           "dds_dir": dds_dir.replace("\\", "/"), "targets": [], "errors": [],
           "cleanup": [], "kept": os.environ.get("PRISM_PROBE_KEEP") == "1"}
    try:
        res["engine"] = unreal.SystemLibrary.get_engine_version()
    except Exception as exc:
        res["errors"].append("engine_version: %r" % (exc,))
    made = []
    try:
        for t in truth["targets"]:
            ok, note = _import_target(t["path"], t["name"])
            if ok:
                made.append(FOLDER + "/" + t["name"])
            row = dict(t)
            row["imported"] = ok
            row["import_note"] = note
            res["targets"].append(row)

        metrics = {}
        try:
            out = dm.get_asset_metrics(
                asset_paths=[FOLDER + "/" + t["name"] for t in res["targets"]],
                max_assets=64, probe_source=True)
            for it in out.get("items") or []:
                metrics[it.get("package_name") or it.get("query")] = it
            if not out.get("items"):
                res["errors"].append("get_asset_metrics 空返回: %s" % str(out)[:300])
        except Exception as exc:
            res["errors"].append("get_asset_metrics: %r" % (exc,))
            res["traceback"] = traceback.format_exc()

        for row in res["targets"]:
            it = metrics.get(FOLDER + "/" + row["name"])
            if it is None:
                row.setdefault("class_note", "no_metrics_item")
                continue
            try:
                _measure_row(st, row, it)
            except Exception as exc:
                res["errors"].append("measure %s: %r" % (row["name"], exc))
                row["measure_failed"] = True
            row["api_surface"] = _api_surface(FOLDER + "/" + row["name"])
        _dump(res, out_path)
        _report(res)
    finally:
        if res["kept"]:
            res["cleanup"] = [["skipped", True]]
        else:
            for pkg in made:
                try:
                    res["cleanup"].append([pkg, bool(unreal.EditorAssetLibrary.delete_asset(pkg))])
                except Exception as exc:
                    res["cleanup"].append([pkg, "err:%r" % (exc,)])
        _dump(res, out_path)
    _say("PROBE -> " + out_path)
    if res["errors"]:
        _say("PROBE ERRORS: %s" % (res["errors"],))


try:
    _main()
except Exception:
    _say(traceback.format_exc())