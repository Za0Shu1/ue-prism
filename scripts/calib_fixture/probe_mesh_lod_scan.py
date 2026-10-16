# -*- coding: utf-8 -*-
"""LOD 1a 真机探针：scan_mesh_lod 的读面命中、判据档位与阈值判别（不进 59 项门禁）。

为什么单独跑：把这条并进 59 项会让它牵动孤儿/引用/预算那几组已知真值（多造一个网格副本，
used_by 与目录预算的读数就可能跟着动）。这里自建真值、自清理，只钉三件事：
1) 读面在真引擎上命中哪条签名（api.hits）——跨版本兼容矩阵就看这张表；
2) 已知真值能否复现：Cube 副本 = 1 级 LOD / 48 三角 / auto_computed True / min_lod 未覆盖；
   探针靶把 ALL 平台 Min LOD 写成 2 后，扫描必须读回 2 且不再报 min_lod_unset；
3) 阈值判别性：同一个网格在 min_triangles=1 下必须翻成 no_lod_highpoly 并进候选，
   在默认阈值下必须是 ok——判据不是常量，是真的在读实测面数。

多级 LOD 靶是机会性构造（`probe_mesh_lod_api.py` 实证 StaticMesh 没留 Python 造 LOD 的面）：
造得出来就自证一致性（ratio/建议级/收益数字与自己测到的行相符），造不出来记 skipped 不记 FAIL。

跑法（无头 commandlet 即可）：
    UnrealEditor-Cmd.exe <uproject> -run=pythonscript ^
        -script=scripts/calib_fixture/probe_mesh_lod_scan.py -stdout -unattended -nosplash
环境变量：PRISM_PROBE_OUT=<输出目录>（默认 <工程>/Saved/PrismFixture）
          PRISM_PROBE_KEEP=1（保留探针资产；默认删除，删除前先释放引用再 gc——
          5.4 实证 python 仍持对象时 delete_asset 会空指针崩编辑器）
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
BASE_CUBE = "/Engine/BasicShapes/Cube"
CALIB_MESH = FOLDER + "/CalibMesh"
PROBE_MINLOD = FOLDER + "/SM_LodScanMinLod"
PROBE_MULTI = FOLDER + "/SM_LodScanMulti"
MINLOD_VALUE = 2
OUT_NAME = "probe_mesh_lod_scan.json"
REP = None   # 崩溃兜底也要把已跑出的 checks 落盘（半截证据比没证据强）
MULTI_APIS = (
    ("mesh", "build_from_mesh_descriptions"),
    ("mesh", "acquire_mesh_description"),
    ("mesh", "release_mesh_description"),
    ("mesh", "set_num_lods"),
    ("class", "StaticMeshEditorSubsystem"),
    ("class", "MeshDescriptionOperationsSubsystem"),
    ("class", "BlueprintMeshDescriptionLibrary"),
)


class Reporter(object):
    def __init__(self):
        self.rows = []
        self.started = time.time()

    def check(self, name, ok, detail=""):
        self.rows.append({"name": name, "pass": bool(ok),
                          "detail": _cap(detail), "kind": "check"})
        _say("%s %s  %s" % ("PASS" if ok else "FAIL", name, _cap(detail, 160)))
        return bool(ok)

    def note(self, name, detail=""):
        self.rows.append({"name": name, "pass": None, "detail": _cap(detail), "kind": "note"})
        _say("NOTE  %s  %s" % (name, _cap(detail, 160)))


def _say(line):
    print(str(line))
    sys.stdout.flush()


def _engine_version():
    try:
        return str(unreal.SystemLibrary.get_engine_version())
    except Exception as exc:
        return "err:" + repr(exc)[:120]


def _cap(value, n=300):
    s = repr(value)
    return s if len(s) <= n else s[:n] + "...(cap)"


def _load(pkg):
    for call in (lambda: unreal.EditorAssetLibrary.load_asset(pkg),
                 lambda: unreal.load_asset(pkg)):
        try:
            obj = call()
        except Exception:
            continue
        if obj is not None:
            return obj
    return None


def _registry_scan(folder):
    """尽量让 AssetRegistry 认识刚落盘的靶（commandlet 里可能还没扫）。"""
    tried = []
    try:
        ar = unreal.AssetRegistryHelpers.get_asset_registry()
    except Exception as exc:
        return {"ar": None, "tried": tried, "err": type(exc).__name__}
    for name in ("scan_paths_synchronous", "scan_paths"):
        fn = getattr(ar, name, None)
        if not callable(fn):
            tried.append({"sig": name, "err": "absent"})
            continue
        for args in (([folder], ), (folder, ), ([folder], True)):
            try:
                fn(*args)
                tried.append({"sig": name + _shape(args), "err": ""})
                return {"ar": ar, "tried": tried, "err": None}
            except Exception as exc:
                tried.append({"sig": name + _shape(args), "err": type(exc).__name__})
    return {"ar": ar, "tried": tried, "err": "no_scan_hit"}


def _shape(args):
    return "(" + ",".join("list" if isinstance(a, (list, tuple)) else type(a).__name__
                          for a in args) + ")"


def load_bytes(path):
    with io.open(path, "rb") as f:
        return f.read()


def _repo_bases():
    """从探针脚本所在目录反推仓库根（真机跑时脚本就在仓库里）。"""
    here = os.path.dirname(os.path.abspath(__file__))
    out = []
    for _ in range(4):
        out.append(here)
        here = os.path.dirname(here)
    return out


def _ensure_prism_importable(rep):
    """插件里的引擎侧模块闭包就是 domain 的落盘形态：探针直连它，测的是真装配产物。"""
    content = unreal.Paths.project_content_dir()
    proj = os.path.dirname(str(content).rstrip("/\\"))
    py_dir = os.path.join(proj, "Plugins", "UEPrism", "Content", "Python")
    rep.note("prism.py_dir", py_dir)
    if not os.path.isdir(py_dir):
        return None
    if py_dir not in sys.path:
        sys.path.insert(0, py_dir)
    import prism.domain as dom           # noqa: F401
    import prism.domain.opt_lod as ol    # noqa: F401
    return ol, dom, py_dir


def _make_minlod_target(rep):
    """Cube 副本 + 已知 Min LOD（ALL=2）：读面必须有非 -1 的真值可对。"""
    EAL = unreal.EditorAssetLibrary
    _drop(PROBE_MINLOD)
    obj = EAL.duplicate_asset(BASE_CUBE, PROBE_MINLOD)
    if obj is None:
        rep.check("target.duplicate", False, PROBE_MINLOD)
        return None
    obj = _load(PROBE_MINLOD)
    tried = []
    written = None
    for label, call in (("set_minimum_lod_for_platform(ALL,int)",
                         lambda m: m.set_minimum_lod_for_platform("ALL", MINLOD_VALUE)),
                        ("set_minimum_lod_for_platforms(map)", None)):
        if call is None:
            continue
        try:
            call(obj)
            tried.append({"sig": label, "err": ""})
            written = label
            break
        except Exception as exc:
            tried.append({"sig": label, "err": type(exc).__name__})
    back = None
    try:
        back = int(obj.get_minimum_lod_for_platform("ALL"))
    except Exception as exc:
        tried.append({"sig": "get_minimum_lod_for_platform(ALL)", "err": type(exc).__name__})
    rep.note("target.write_tried", tried)
    saved, stried = _save_asset(EAL, PROBE_MINLOD)
    rep.note("target.save_tried", stried)
    ok = written is not None and back == MINLOD_VALUE
    rep.check("target.minlod_writeback", ok,
              {"via": written, "readback": back, "want": MINLOD_VALUE, "saved": saved})
    obj = None
    gc.collect()
    return PROBE_MINLOD


def _try_multi_lod_target(rep):
    """机会性构造 >=2 级 LOD 的靶；API 不通就诚实记 skipped（立项前探针已知这面很窄）。"""
    EAL = unreal.EditorAssetLibrary
    _drop(PROBE_MULTI)
    if EAL.duplicate_asset(BASE_CUBE, PROBE_MULTI) is None:
        return None
    mesh = _load(PROBE_MULTI)
    avail = {}
    for kind, name in MULTI_APIS:
        holder = mesh if kind == "mesh" else unreal
        avail[name] = callable(getattr(holder, name, None))
    rep.note("multi.api_surface", avail)
    built = None
    tried = []
    try:
        if avail.get("acquire_mesh_description") and avail.get("build_from_mesh_descriptions"):
            desc = mesh.acquire_mesh_description()
            got = mesh.build_from_mesh_descriptions([desc, desc])
            tried.append({"sig": "acquire + build_from_mesh_descriptions([d,d])",
                          "ret": repr(got)[:80]})
            try:
                mesh.release_mesh_description(desc, True)
            except Exception:
                pass
            built = int(mesh.get_num_lods())
    except Exception as exc:
        tried.append({"sig": "acquire+build", "err": "%s:%s" % (type(exc).__name__, str(exc)[:120])})
    if built is None:
        for name in ("set_num_lods", "resize_lod", "regenerate_lods"):
            fn = getattr(mesh, name, None)
            if not callable(fn):
                continue
            for args, label in (((2, ), "int"), ):
                try:
                    fn(*args)
                    tried.append({"sig": name + label, "err": ""})
                    built = int(mesh.get_num_lods())
                    break
                except Exception as exc:
                    tried.append({"sig": name + label, "err": type(exc).__name__})
            if built is not None:
                break
    rep.note("multi.build_tried", tried)
    if not built or built < 2:
        rep.note("multi.target", "skipped: 造不出 >=2 级 LOD（写面不承诺，判据由 L0 钉）")
        _drop(PROBE_MULTI)
        return None
    try:
        EAL.save_asset(PROBE_MULTI, only_if_loaded=False)
    except Exception:
        pass
    rep.check("multi.target_built", True, "num_lods=%s" % built)
    obj = None
    gc.collect()
    return PROBE_MULTI


def _save_asset(EAL, pkg):
    """save_asset 的签名面跨版本漂（5.4 实测关键字 only_if_loaded 直接 TypeError）。"""
    tried = []
    for label, call in (
        ("save_asset(pkg)", lambda: EAL.save_asset(pkg)),
        ("save_asset(pkg, False)", lambda: EAL.save_asset(pkg, False)),
        ("save_asset(FName(pkg))", lambda: EAL.save_asset(unreal.Name(pkg))),
    ):
        try:
            got = call()
            tried.append({"sig": label, "ret": repr(got)[:40]})
            return bool(got) or got is None, tried
        except Exception as exc:
            tried.append({"sig": label, "err": type(exc).__name__})
    return False, tried


def _drop(pkg):
    EAL = unreal.EditorAssetLibrary
    obj = None
    try:
        if EAL.does_asset_exist(pkg):
            EAL.delete_asset(pkg)
    except Exception as exc:
        _say("drop %s err %s" % (pkg, type(exc).__name__))
    gc.collect()


def _item_of(res, pkg):
    for it in res.get("items") or []:
        if it.get("package") == pkg:
            return it
    return None


def _in_candidates(res, key, pkg):
    return any(c.get("package") == pkg for c in (res.get("candidates") or {}).get(key) or [])


def main():
    global REP
    rep = Reporter()
    REP = rep
    out = {"ue_version": None, "started": time.strftime("%Y-%m-%dT%H:%M:%S"),
           "checks": rep.rows}
    out["ue_version"] = _engine_version()

    ol_dom = None
    try:
        ol_dom = _ensure_prism_importable(rep)
    except Exception as exc:
        rep.check("import.prism_domain", False, "%s:%s" % (type(exc).__name__, str(exc)[:200]))
    if ol_dom is None:
        _finish(rep, out, None)
        return

    ol, dom, py_dir = ol_dom
    rep.check("domain.registered", "scan_mesh_lod" in dom.FUNCTIONS,
              sorted([k for k in dom.FUNCTIONS if "lod" in k]))
    # 防假绿：插件里那份必须和仓库工作副本逐字节一致，否则测的是旧代码
    repo_copy = None
    for root in (os.path.dirname(os.path.dirname(py_dir)), py_dir):
        cand = os.path.join(root, "prism", "domain", "opt_lod.py")
        if os.path.isfile(cand):
            repo_copy = cand
            break
    same = None
    if repo_copy:
        installed = load_bytes(repo_copy)
        for base in _repo_bases():
            src = os.path.join(base, "prism", "domain", "opt_lod.py")
            if os.path.isfile(src):
                same = load_bytes(src) == installed
                rep.note("plugin_copy_matches_repo", {"src": src, "same": same})
                break
    rep.check("plugin.copy_current", same is not False,
              {"checked": same is not None, "why": "仓库副本没找到就只记 note，不冤枉真机"})

    reg = _registry_scan(FOLDER)
    out["registry_scan"] = {"tried": reg.get("tried"), "err": reg.get("err")}
    rep.note("registry.scan", reg.get("err") or reg.get("tried"))

    # 靶不存在（夹具没跑过）就自己造一个 Cube 副本当 CalibMesh 用
    if not unreal.EditorAssetLibrary.does_asset_exist(CALIB_MESH):
        unreal.EditorAssetLibrary.duplicate_asset(BASE_CUBE, CALIB_MESH)
        _registry_scan(FOLDER)
        rep.note("fallback_target", "CalibMesh 不在，现场造副本（真值仍为 1 级 / 48 三角）")

    # ---------- 默认阈值：已知真值复现 ----------
    res = ol.scan_mesh_lod(paths=CALIB_MESH)
    out["scan_default"] = res
    hits_by_scan = {"default": (res.get("api") or {}).get("hits") or {}}
    out["api_hits_by_scan"] = hits_by_scan
    if res.get("ok") is False:
        rep.check("scan.envelope", False, res.get("error"))
        _finish(rep, out, res)
        return
    it = _item_of(res, CALIB_MESH)
    rep.check("scan.structure_keys",
              {"scope", "thresholds", "summary", "items", "candidates", "total",
               "truncated", "cap", "api", "rejected"} <= set(res), sorted(res))
    rep.check("scan.target_found", it is not None, {"total": res.get("total"),
                                                    "rejected": res.get("rejected")})
    if it:
        rep.check("truth.num_lods", it.get("num_lods") == 1, it.get("num_lods"))
        rep.check("truth.triangles", it.get("triangles") == 48, it.get("triangles"))
        rep.check("truth.auto_computed", it.get("auto_computed") is True, it.get("auto_computed"))
        rep.check("truth.category_ok", it.get("category") == "ok",
                  {"category": it.get("category"), "flags": it.get("flags")})
        rep.check("truth.min_lod_unset_flag", "min_lod_unset" in (it.get("flags") or []),
                  it.get("flags"))
        rep.check("truth.no_suggestion", it.get("suggested_min_lod") is None
                  and it.get("potential_triangles") == 0,
                  {"suggested": it.get("suggested_min_lod"),
                   "potential": it.get("potential_triangles")})
        rep.check("truth.not_force_min_lod_candidate",
                  not _in_candidates(res, "force_min_lod", CALIB_MESH)
                  and not _in_candidates(res, "weak_lod_chain", CALIB_MESH),
                  res.get("candidates"))
        hits = (res.get("api") or {}).get("hits") or {}
        out["api_hits"] = hits
        rep.check("api.read_face", "get_num_lods:noarg" in hits
                  and "is_lod_screen_size_auto_computed:noarg" in hits
                  and any(k.startswith("get_minimum_lod") for k in hits), hits)

    # ---------- 阈值判别性：同一网格换个阈值必须换档 ----------
    res_low = ol.scan_mesh_lod(paths=CALIB_MESH, min_triangles=1)
    it_low = _item_of(res_low, CALIB_MESH)
    out["scan_min_tri_1"] = res_low
    rep.check("threshold.discriminates", it_low is not None
              and it_low.get("category") == "no_lod_highpoly"
              and _in_candidates(res_low, "no_lod_highpoly", CALIB_MESH),
              {"category": (it_low or {}).get("category"),
               "candidates": res_low.get("candidates")})
    rep.check("threshold.no_free_numbers", (it_low or {}).get("potential_triangles") == 0,
              (it_low or {}).get("potential_triangles"))

    # ---------- 已覆盖 Min LOD 的靶：读面必须读到非 -1 真值 ----------
    minlod_pkg = _make_minlod_target(rep)
    if minlod_pkg:
        res2 = ol.scan_mesh_lod(paths=minlod_pkg)
        out["scan_minlod_target"] = res2
        hits_by_scan["minlod_target"] = (res2.get("api") or {}).get("hits") or {}
        it2 = _item_of(res2, minlod_pkg)
        if rep.check("minlod.target_scanned", it2 is not None, res2.get("rejected")):
            got = (it2.get("min_lod_by_platform") or {})
            rep.check("minlod.readback", got.get("ALL") == MINLOD_VALUE, got)
            rep.check("minlod.no_unset_flag", "min_lod_unset" not in (it2.get("flags") or []),
                      it2.get("flags"))
            rep.check("minlod.summary_covered",
                      (res2.get("summary") or {}).get("min_lod_covered") == 1,
                      res2.get("summary"))

    # ---------- 机会性多 LOD 靶：造得出就自证一致性 ----------
    multi_pkg = _try_multi_lod_target(rep)
    if multi_pkg:
        res3 = ol.scan_mesh_lod(paths=multi_pkg, min_triangles=1)
        out["scan_multi"] = res3
        hits_by_scan["multi"] = (res3.get("api") or {}).get("hits") or {}
        it3 = _item_of(res3, multi_pkg)
        if rep.check("multi.scanned", it3 is not None, res3.get("rejected")):
            lods = it3.get("lods") or []
            tris = [int(r.get("triangles") or 0) for r in lods if r.get("triangles") is not None]
            ratio = it3.get("lod1_ratio")
            want = round(float(tris[1]) / float(tris[0]), 4) if len(tris) >= 2 and tris[0] else None
            rep.check("multi.ratio_self_consistent", ratio == want, {"got": ratio, "want": want})
            rep.check("multi.category_valid",
                      it3.get("category") in ("weak_lod_chain", "min_lod_actionable", "ok",
                                              "unreadable", "nanite_managed"),
                      it3.get("category"))
            sug = it3.get("suggested_min_lod")
            if it3.get("category") in ("weak_lod_chain", "min_lod_actionable"):
                rep.check("multi.suggest_is_last_lod", sug == it3.get("num_lods") - 1, sug)
                rep.check("multi.potential_or_flag",
                          it3.get("potential_triangles") > 0
                          or "reduction_unmeasured" in (it3.get("flags") or []),
                          {"potential": it3.get("potential_triangles"), "flags": it3.get("flags")})

    # ---------- 坏路径诚实性 ----------
    res_bad = ol.scan_mesh_lod(paths=FOLDER + "/SM_NotHereAtAll")
    out["scan_bad_path"] = res_bad
    rep.check("badpath.rejected_not_ok", res_bad.get("total") == 0
              and bool(res_bad.get("rejected"))
              and res_bad.get("note") != "no_engine",
              {"total": res_bad.get("total"), "rejected": res_bad.get("rejected"),
               "note": res_bad.get("note")})

    if not os.environ.get("PRISM_PROBE_KEEP"):
        for pkg in (PROBE_MINLOD, PROBE_MULTI):
            _drop(pkg)
    _finish(rep, out, None)


def _finish(rep, out, _res):
    fails = [r for r in rep.rows if r.get("pass") is False]
    passed = [r for r in rep.rows if r.get("pass") is True]
    out["verdict"] = "PASS" if not fails else "FAIL"
    out["counts"] = {"pass": len(passed), "fail": len(fails),
                     "note": len([r for r in rep.rows if r.get("kind") == "note"])}
    out["fails"] = [r["name"] for r in fails]
    out["elapsed_s"] = round(time.time() - rep.started, 2)
    try:
        content = unreal.Paths.project_content_dir()
        out_dir = os.environ.get("PRISM_PROBE_OUT") or os.path.join(
            os.path.dirname(str(content).rstrip("/\\")), "Saved", "PrismFixture")
    except Exception:
        out_dir = os.environ.get("PRISM_PROBE_OUT") or os.getcwd()
    if not os.path.isdir(out_dir):
        os.makedirs(out_dir)
    tmp = os.path.join(out_dir, OUT_NAME + ".tmp")
    final = os.path.join(out_dir, OUT_NAME)
    with io.open(tmp, "w", encoding="utf-8", newline="") as f:
        json.dump(out, f, indent=2, ensure_ascii=False, default=str)
    os.replace(tmp, final)
    _say("LODSCAN-PROBE verdict=%s pass=%d fail=%d note=%d -> %s"
         % (out["verdict"], out["counts"]["pass"], out["counts"]["fail"],
            out["counts"]["note"], final))
    sys.stdout.flush()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        err = traceback.format_exc()
        _say("LODSCAN-PROBE CRASH " + err)
        try:
            content = unreal.Paths.project_content_dir()
            d = os.environ.get("PRISM_PROBE_OUT") or os.path.join(
                os.path.dirname(str(content).rstrip("/\\")), "Saved", "PrismFixture")
            if not os.path.isdir(d):
                os.makedirs(d)
            with io.open(os.path.join(d, OUT_NAME + ".tmp"), "w", encoding="utf-8", newline="") as f:
                rows = REP.rows if REP is not None else []
                json.dump({"verdict": "CRASH", "traceback": err, "checks": rows,
                           "counts": {"fail": len([r for r in rows
                                                   if r.get("pass") is False])}},
                          f, indent=2, ensure_ascii=False, default=str)
            os.replace(os.path.join(d, OUT_NAME + ".tmp"), os.path.join(d, OUT_NAME))
        except Exception:
            pass
        raise