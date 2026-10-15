# -*- coding: utf-8 -*-
"""#11 B2/C 真实工程只读复测（在 UE 内跑；全程无 save/delete/写资产）。

回答两个悬而未决的口径问题（一次取证）：
1) **B2 面数语义**：真 `TextureCube` 的 `source_memory_bytes` 到底含几个面——
   夹具的 DDS 靶（Texture2DArray 类）是 6 面全算，而真实工程那张 4096 立方
   反推成 2365 只能由「只算一面」解释。两边口径只能活一个。
2) **C 命中率**：`asset_import_data` 记录的原始文件路径上，原文件还在不在；
   在的话容器头宽高与 `src_mem` 能否互验（1 面 x6 面 x BPT 直接解出）。

枚举当前工程 /Game 的全部 TextureCube（按 uasset 盘大小降序，默认最多 16 张），
走域层真管线（仓库当前代码，不是插件旧拷贝）读冷态度量，逐张附导入记录与
原文件头解析。产出 recheck_cube_semantics.json + unreal.log 表。

环境变量：PRISM_RECHECK_OUT=<输出目录>（必填，建议指到工程外的取证目录）
          PRISM_RECHECK_MAX=<张数上限>（默认 16）
          PRISM_RECHECK_REPO=<含 prism 包的仓库目录>（缺省自动向上找）
用法（命令行）：
  UnrealEditor-Cmd.exe <uproject> -run=pythonscript \\
      -script=<repo>/scripts/recheck_cube_semantics.py -stdout -unattended -nosplash
"""
import json
import os
import sys
import traceback

import unreal

OUT_NAME = "recheck_cube_semantics.json"
_ss = None


def _say(line):
    try:
        unreal.log(str(line))
    except Exception:
        print(str(line))


def _here():
    return os.path.dirname(os.path.abspath(__file__))


def _load_prism():
    """取证要用仓库当前生产代码（含 A″ 阀字段），优先于工程里可能滞后的插件拷贝。"""
    cands = []
    env = os.environ.get("PRISM_RECHECK_REPO")
    if env:
        cands.append(env)
    d = _here()
    for _ in range(4):
        d = os.path.dirname(d)
        cands.append(d)
    for cand in cands:
        if cand and os.path.isdir(os.path.join(cand, "prism")):
            stale = [m for m in list(sys.modules)
                     if m == "prism" or m.startswith("prism.")]
            if stale:
                loaded = getattr(sys.modules.get("prism"), "__file__", "") or ""
                if not os.path.normcase(loaded).startswith(os.path.normcase(cand)):
                    for m in stale:
                        del sys.modules[m]
            if cand not in sys.path:
                sys.path.insert(0, cand)
            from prism.domain import metrics as dm
            from prism import streaming as st
            sys.path.insert(0, os.path.join(_here(), "calib_fixture"))
            import source_scan as ss
            return dm, st, ss, cand
    raise RuntimeError("找不到 prism 包（设 PRISM_RECHECK_REPO=<仓库目录>）")


def _list_cubes():
    ar = unreal.AssetRegistryHelpers.get_asset_registry()
    paths = []
    try:
        datas = ar.get_assets_by_class(
            unreal.TopLevelAssetPath("/Script/Engine", "TextureCube"), True) or []
    except Exception as exc:
        return [], "get_assets_by_class:%r" % (exc,)
    for d in datas:
        p = str(d.package_name)
        if p.startswith("/Game/"):
            paths.append(p)
    return sorted(paths), None


def _disk_sorted(pkg_paths, cap):
    rows = []
    for p in pkg_paths:
        f = os.path.join(str(unreal.SystemLibrary.get_project_directory()),
                         "Content", p[len("/Game/"):].replace("/", os.sep) + ".uasset")
        try:
            sz = os.path.getsize(f)
        except OSError:
            sz = -1
        rows.append((sz, p, f))
    rows.sort(key=lambda r: -r[0])
    return rows[:cap], len(rows)


def _import_record(pkg):
    """C 面：导入记录路径 + 原文件在场性 + 容器头真值。"""
    out = {"recorded": [], "note": None}
    obj = None
    try:
        obj = unreal.load_asset(pkg)
    except Exception as exc:
        out["note"] = "load_err:%r" % (exc,)
        return obj, out
    if obj is None:
        out["note"] = "load_none"
        return obj, out
    try:
        si = obj.get_editor_property("asset_import_data")
    except Exception:
        si = None
    if si is None:
        out["note"] = "no_asset_import_data（未走导入或旧管线）"
        return obj, out
    files = []
    for meth in ("extract_filenames", "script_extract_filenames",
                 "get_first_filename", "script_get_first_filename"):
        fn = getattr(si, meth, None)
        if not callable(fn):
            continue
        try:
            v = fn()
        except Exception as exc:
            out.setdefault("errs", []).append("%s:%r" % (meth, exc))
            continue
        if isinstance(v, str):
            v = [v]
        if v:
            files = [str(x) for x in v]
            out["via"] = meth
            break
    out["recorded"] = files
    infos = []
    for f in files:
        entry = {"path_tail": f.replace("\\", "/").rsplit("/", 1)[-1],
                 "abs": f.replace("\\", "/")}
        try:
            entry["exists"] = os.path.isfile(f)
        except Exception as exc:
            entry["exists"] = "err:%r" % (exc,)
            infos.append(entry)
            continue
        if entry["exists"]:
            try:
                entry["bytes"] = os.path.getsize(f)
                head = _head_of(f)
                if head:
                    entry["header"] = head
            except Exception as exc:
                entry["head_err"] = repr(exc)
        infos.append(entry)
    out["recorded_info"] = infos
    return obj, out


def _head_of(path):
    """原文件头（宽松版）：读前 128KB 就够解出宽高，无需整读大图。"""
    ext = os.path.splitext(path)[1].lower()
    try:
        with open(path, "rb") as fh:
            data = fh.read(131072)
    except Exception as exc:
        return {"err": repr(exc)}
    if ext == ".hdr":
        return _ss.parse_hdr(data, 0)
    if ext == ".dds":
        return _ss.parse_dds(data, 0)
    if ext == ".png":
        return _ss.parse_png(data, 0)
    if ext in (".jpg", ".jpeg"):
        return _ss.parse_jpeg(data, 0)
    if ext == ".bmp" and data[:2] == b"BM" and len(data) >= 30:
        hs = int.from_bytes(data[14:18], "little")
        if hs in (12, 40, 56, 108, 124):
            if hs == 12:
                w = int.from_bytes(data[18:20], "little")
                h = int.from_bytes(data[20:22], "little")
                bpp = int.from_bytes(data[24:26], "little")
            else:
                w = int.from_bytes(data[18:22], "little", signed=True)
                h = int.from_bytes(data[22:26], "little", signed=True)
                bpp = int.from_bytes(data[28:30], "little")
            return {"kind": "bmp", "w": abs(w), "h": abs(h), "bpp": bpp}
    return {"kind": "unparsed_ext", "ext": ext}


def _faces_for(st, cls, item):
    try:
        arr = int((item or {}).get("array_size") or 0)
    except (TypeError, ValueError):
        arr = 0
    try:
        return st.faces_for(cls, arr or None)
    except Exception:
        return None


def _main():
    out_dir = os.environ.get("PRISM_RECHECK_OUT")
    if not out_dir:
        _say("PRISM_RECHECK_OUT 未设置，拒绝把取证写进被检工程目录")
        return
    cap = 16
    try:
        cap = max(1, int(os.environ.get("PRISM_RECHECK_MAX") or 16))
    except ValueError:
        pass
    global _ss
    dm, st, _ss, prism_src = _load_prism()
    res = {"engine": unreal.SystemLibrary.get_engine_version(),
           "prism_src": prism_src.replace("\\", "/"),
           "pool_model": st.POOL_MODEL_VERSION,
           "targets": [], "errors": [], "enum_note": None}
    try:
        res["out_dir_check"] = os.path.normcase(out_dir) != os.path.normcase(
            str(unreal.SystemLibrary.get_project_directory()))
    except Exception:
        pass
    cubes, enum_err = _list_cubes()
    res["cubes_found"] = len(cubes)
    if enum_err:
        res["enum_note"] = enum_err
    sized, total = _disk_sorted(cubes, cap)
    paths = [p for _sz, p, _f in sized]
    if not paths:
        _dump(res, out_dir)
        _say("RECHECK: 工程内无 /Game TextureCube 靶")
        return
    try:
        mout = dm.get_asset_metrics(asset_paths=paths, max_assets=cap, probe_source=True)
        items = {it.get("package_name") or it.get("query"): it
                 for it in (mout.get("items") or [])}
    except Exception as exc:
        res["errors"].append("get_asset_metrics:%r" % (exc,))
        items = {}
    for sz, pkg, _f in sized:
        it = items.get(pkg) or {}
        mm = it.get("metrics") or it
        cls = it.get("class") or mm.get("class") or ""
        faces = _faces_for(st, cls, it)
        src = mm.get("source_memory_bytes")
        derived = st.source_side_from_memory(src, faces or 1) if src else None
        obj, rec = _import_record(pkg)
        row = {"package": pkg, "uasset_bytes": sz, "class": cls,
               "compression": str(mm.get("compression") or ""),
               "src_mem": mm.get("source_memory_bytes"),
               "src_disk": mm.get("source_disk_bytes"),
               "max_size": mm.get("max_size"),
               "array_size": mm.get("array_size"),
               "derived_by_domain": [mm.get("source_width"),
                                     mm.get("source_bytes_per_texel"),
                                     mm.get("source_side_exact")],
               "faces_used": faces,
               "derived": (list(derived) if derived else None),
               "recorded_src": rec}
        res["targets"].append(row)
        if rec.get("note"):
            row["src_note"] = rec["note"]
        _drop_asset_ref(obj)
    _dump(res, out_dir)
    _report(res)


def _drop_asset_ref(obj):
    """只读取证不删资产：放掉本轮持有的引用并 GC，避免编辑器内存堆积。"""
    del obj
    try:
        import gc
        gc.collect()
    except Exception:
        pass


def _report(res):
    hdr = "%-42s %-11s %-9s %-12s %-6s %-14s %-5s %-9s" % (
        "package", "class", "srcmem", "derived", "faces", "src_file", "live", "hdr")
    _say(hdr)
    for r in res["targets"]:
        info = (r["recorded_src"].get("recorded_info") or [{}])[0]
        head = info.get("header") or {}
        _say("%-42s %-11s %-9s %-12s %-6s %-14s %-5s %-9s" % (
            r["package"][-42:], r["class"], r["src_mem"],
            "%s@%s/%s" % ((r["derived"] or [None, None, None, None])[0],
                          (r["derived"] or [None, None, None, None])[1],
                          (r["derived"] or [None, None, None, None])[3]),
            r["faces_used"], str(info.get("path_tail"))[:14],
            str(bool(info.get("exists"))).lower(),
            "%sx%s" % (head.get("w", "-"), head.get("h", "-"))))
    _say("RECHECK engine=%s cubes=%d targets=%d errors=%s" % (
        res["engine"], res.get("cubes_found", 0), len(res["targets"]),
        res["errors"] or "none"))


def _dump(res, out_dir):
    if not os.path.isdir(out_dir):
        os.makedirs(out_dir)
    p = os.path.join(out_dir, OUT_NAME)
    try:
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(res, fh, ensure_ascii=False, indent=1)
        _say("RECHECK -> " + p)
    except Exception as exc:
        res["errors"].append("dump:%r" % (exc,))
        _say("RECHECK dump failed: %r" % (exc,))


def _fail(exc_text):
    """取证工具的兜底：异常必须落盘，不能只进不可见的引擎日志。"""
    _say(exc_text)
    out_dir = os.environ.get("PRISM_RECHECK_OUT") or ""
    try:
        if out_dir:
            if not os.path.isdir(out_dir):
                os.makedirs(out_dir)
            with open(os.path.join(out_dir, "recheck_error.txt"), "w", encoding="utf-8") as fh:
                fh.write(exc_text)
    except Exception:
        pass


try:
    _main()
except Exception:
    _fail(traceback.format_exc())