# -*- coding: utf-8 -*-
"""#11 C 可行性探针（在 UE 内跑；只读度量 + 自清理；不进 59 项夹具门禁）。

B1 之后 C（"直读源图拿真边长"）只剩一个没量过的面：**源图的真宽高到底还留在哪**。
本探针把 make_dds 的已知真值靶导入沙箱后，三个方向同时取证：

1) 对象面：纹理对象上 import/source 类属性（asset_import_data/source_data/...）
   在 5.3/5.4 的 Python 上到不到得着；拿到的对象再钻一层（记录的原始文件路径、
   源数据字节在不在）。
2) 注册表/包面：`<Name>_Source` 元数据对象是否存在、可否按对象路径解析；
   AssetData 标签里有没有源文件线索。
3) 包文件面：uasset 落盘字节里有没有源图容器头（PNG/JPEG/BMP/DDS 签名 +
   结构自洽校验）与 SourceFileData 类名字表痕迹——判据在纯 stdlib 的
   source_scan.py，离线可测（tests/test_source_scan.py）。

产物 probe_source_artifacts.json 只陈述"哪条路到得着/到不着"（tried+reason），
不猜。跑法与环境变量同 probe_source_truth.py（PRISM_PROBE_DDS/OUT/KEEP）。
"""
import json
import os
import sys
import traceback

import unreal

FOLDER = "/Game/PrismCalib"
OUT_NAME = "probe_source_artifacts.json"

PROP_TRIALS = ("asset_import_data", "import_data", "source_data",
               "source_file", "source_artifacts", "import_settings",
               "legacy_import_settings")
UNREAL_API_CANDIDATES = ("AssetImportData", "ObjectInterchangeSourceData",
                         "InterchangeSourceData", "TextureSourceData",
                         "PackageToolsEditor", "PackageExporter",
                         "PackageExportSettings", "ObjectTools",
                         "InterchangeReimportHandler", "AssetExportData")


def _say(line):
    try:
        unreal.log(str(line))
    except Exception:
        print(str(line))


def _cap(text, n):
    text = str(text).replace("\r\n", " ").replace("\n", " ")
    return text if len(text) <= n else text[:n] + "..."


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


def _import_target(path, name):
    task = unreal.AssetImportTask()
    task.set_editor_property("filename", path)
    task.set_editor_property("destination_path", FOLDER)
    task.set_editor_property("destination_name", name)
    task.set_editor_property("automated", True)
    task.set_editor_property("replace_existing", True)
    task.set_editor_property("save", True)
    try:
        unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks([task])
    except Exception as exc:
        return False, "import_exception: %r" % (exc,)
    try:
        return bool(unreal.EditorAssetLibrary.does_asset_exist(FOLDER + "/" + name)), ""
    except Exception as exc:
        return False, "does_asset_exist:%r" % (exc,)


def _try_props(obj):
    """对象面：逐个试 import/source 类属性，命中记类型与内容摘要。"""
    got = {}
    for name in PROP_TRIALS:
        try:
            value = obj.get_editor_property(name)
        except Exception:
            got[name] = "miss"
            continue
        if value is None:
            got[name] = "None"
        else:
            got[name] = type(value).__name__ + ":" + _cap(repr(value), 160)
    return got


def _dive(si):
    """钻 import/source 数据对象：属性清单、文件路径、能调就调。"""
    low = [n for n in dir(si) if not n.startswith("_")]
    info = {"pytype": type(si).__name__,
            "attrs": [n for n in low if ("sourc" in n.lower() or "file" in n.lower()
                                         or "import" in n.lower())][:24]}
    for prop in ("filenames", "file_names", "source_files", "source_file",
                 "source_object"):
        try:
            info["prop:" + prop] = _cap(si.get_editor_property(prop), 200)
        except Exception:
            info["prop:" + prop] = "miss"
    for meth in ("get_source_file", "get_file_names", "get_filenames",
                 "extract_filenames", "get_first_filename",
                 "script_extract_filenames", "script_get_first_filename"):
        fn = getattr(si, meth, None)
        if callable(fn):
            try:
                info["call:" + meth] = _cap(fn(), 200)
            except Exception as exc:
                info["call:" + meth] = "err:%s" % _cap(exc, 80)
    return info


def _registry_probe(pkg, name):
    """注册表/包面：_Source 元数据对象与 AssetData 标签。"""
    out = {}
    try:
        ar = unreal.AssetRegistryHelpers.get_asset_registry()

        def _valid(asset_data):
            try:
                return bool(asset_data.is_valid())
            except Exception:
                return None
        ad = ar.get_asset_by_object_path(pkg)
        out["asset_data_valid"] = _valid(ad)
        sobj = pkg + "." + name + "_Source"
        out["source_in_registry"] = _valid(ar.get_asset_by_object_path(sobj))
        loader = getattr(unreal, "EditorAssetLibrary", None)
        try:
            got = loader.load_asset(sobj) if loader else None
            out["source_load"] = type(got).__name__ if got is not None else "None"
            if got is not None:
                out["source_dive"] = _dive(got)
        except Exception as exc:
            out["source_load"] = "err:%s" % _cap(exc, 100)
    except Exception as exc:
        out["registry_err"] = _cap(exc, 120)
    return out


def _disk_probe(name):
    """包文件面：uasset 字节里的容器头与名字表痕迹（判据在 source_scan）。"""
    here = _here()
    if here not in sys.path:
        sys.path.insert(0, here)
    import source_scan as ss
    path = os.path.join(str(unreal.SystemLibrary.get_project_directory()),
                        "Content", "PrismCalib", name + ".uasset")
    if not os.path.isfile(path):
        return {"path_missing": path}
    with open(path, "rb") as fh:
        data = fh.read()
    return {"bytes": len(data),
            "sig_hits": ss.scan_blob(data),
            "name_tokens": {k: v for k, v in ss.scan_names(data).items() if v}}


def _dump(res, out_path):
    try:
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(res, fh, ensure_ascii=False, indent=1)
    except Exception as exc:
        res["errors"].append("dump: %r" % (exc,))


def _report(res):
    _say("PROBE-ART engine=%s targets=%d" % (res["engine"], len(res["targets"])))
    avail = [k for k, v in res["unreal_api"].items() if v]
    _say("  unreal_api present: %s" % (avail or "none"))
    for r in res["targets"]:
        props = [k for k, v in (r.get("props") or {}).items()
                 if v not in (None, "miss", "None")]
        hits = r.get("disk", {}).get("sig_hits") or []
        toks = list((r.get("disk", {}).get("name_tokens") or {}).keys())
        _say("  %-14s props=[%s] disk=%dB hits=%d tokens=%s src_obj=%s" % (
            r["name"], ",".join(props), r.get("disk", {}).get("bytes", -1),
            len(hits), toks or "-",
            (r.get("registry") or {}).get("source_load", "-")))
        for h in hits[:4]:
            _say("      SIG %-4s @%-10d %sx%s %s" % (
                h["kind"], h["offset"], h.get("w"), h.get("h"),
                _cap({k: v for k, v in h.items()
                      if k not in ("kind", "offset", "w", "h")}, 90)))
    _say("PROBE-ART ERRORS: %s" % (res["errors"] or "none",))


def _main():
    dds_dir = _dds_dir()
    truth, dds_dir = _ensure_dds(dds_dir)
    out_dir = os.environ.get("PRISM_PROBE_OUT") or dds_dir
    if not os.path.isdir(out_dir):
        os.makedirs(out_dir)
    out_path = os.path.join(out_dir, OUT_NAME)
    res = {"engine": None, "dds_dir": dds_dir.replace("\\", "/"),
           "unreal_api": {k: hasattr(unreal, k) for k in UNREAL_API_CANDIDATES},
           "targets": [], "errors": [], "cleanup": [],
           "kept": os.environ.get("PRISM_PROBE_KEEP") == "1"}
    try:
        res["engine"] = unreal.SystemLibrary.get_engine_version()
    except Exception as exc:
        res["errors"].append("engine_version: %r" % (exc,))
    made = []
    try:
        for t in truth["targets"]:
            ok, note = _import_target(t["path"], t["name"])
            row = dict(t)
            row["imported"] = ok
            if note:
                row["import_note"] = note
            if ok:
                made.append(FOLDER + "/" + t["name"])
            res["targets"].append(row)
        for row in res["targets"]:
            pkg = FOLDER + "/" + row["name"]
            if not row["imported"]:
                row.setdefault("class_note", "not_imported")
                continue
            obj = None
            try:
                obj = unreal.load_asset(pkg)
            except Exception as exc:
                res["errors"].append("load %s: %r" % (row["name"], exc))
            if obj is not None:
                row["class"] = type(obj).__name__
                row["props"] = _try_props(obj)
                try:
                    si = obj.get_editor_property("asset_import_data")
                except Exception:
                    si = None
                if si is not None:
                    row["import_data"] = _dive(si)
            row["registry"] = _registry_probe(pkg, row["name"])
            try:
                row["disk"] = _disk_probe(row["name"])
            except Exception as exc:
                res["errors"].append("disk %s: %r" % (row["name"], exc))
            obj = si = None
        _dump(res, out_path)
        _report(res)
    finally:
        # 删除前放掉 python 侧最后一次的强引用：5.4 实测 delete_asset 仍持有
        # obj/si 时在 GatherObjectReferencersForDeletion 空指针崩编辑器。
        try:
            import gc
            gc.collect()
        except Exception:
            pass
        if res["kept"]:
            res["cleanup"] = [["skipped", True]]
        else:
            for pkg in made:
                try:
                    res["cleanup"].append(
                        [pkg, bool(unreal.EditorAssetLibrary.delete_asset(pkg))])
                except Exception as exc:
                    res["cleanup"].append([pkg, "err:%r" % (exc,)])
        _dump(res, out_path)
    _say("PROBE -> " + out_path)


try:
    _main()
except Exception:
    _say(traceback.format_exc())