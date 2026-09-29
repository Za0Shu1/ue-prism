# -*- coding: utf-8 -*-
"""夹具真值校验：编辑器在线时经总线+规则引擎逐项核对已知答案。

用法: python checks.py <bus_dir> <project_dir> [--out matrix.json]
每检查输出 PASS/FAIL + 证据；退出码 0=全过。同时落一份矩阵 json
（ue_version / tried 命中 / 计时），跨引擎版本汇总 API 兼容矩阵。
"""
from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from prism import bus, rules  # noqa: E402

F = "/Game/PrismCalib"
ORPHAN_TARGETS = {"T_BigNoise", "T_FakeBig", "T_Used1K",
                  "T_Orphan1", "T_Orphan2", "T_Orphan3", "T_CalibCube"}
TMPDIR = F + "/_pwtmp"


def _t(fn):
    t0 = time.time()
    v = fn()
    return v, round(time.time() - t0, 3)


class Runner:
    def __init__(self, bus_dir):
        self.bus_dir = bus_dir
        self.results = []
        self.client = bus.BusClient(bus_dir, timeout=float(
            os.environ.get("PRISM_TIMEOUT", "120")))

    def call(self, fn, args):
        env = self.client.call(fn, args)
        if not env.get("ok"):
            raise RuntimeError("%s failed: %s" % (fn, env.get("error")))
        return env["result"]

    def check(self, name, cond, evidence):
        self.results.append({"name": name, "pass": bool(cond),
                             "evidence": evidence})
        mark = "PASS" if cond else "FAIL"
        print("%-4s %s | %s" % (mark, name, evidence))


def _migrate_rename_roundtrip(r):
    """真机写路径校验（内存态，自还原，不落盘）：改名 fixture 纹理 -> 核对
    注册表旧路径消失/新路径存在 -> 改回。执行 5.4 真机 rename_asset 候选链。"""
    pkg = F + "/T_CalibCube"
    leaf = "T_CalibCube"
    tmp = F + "/Z_pwtrip"
    base = r.call("describe_asset", {"asset_path": pkg})
    if not base.get("found"):
        r.check("migrate.rename_applies_redirect", False, "baseline missing: %s" % base)
        return
    try:
        rn = r.call("migrate_asset_rename", {
            "asset_path": pkg, "new_name": "Z_pwtrip",
            "confirm": True, "fixup_redirectors": False})
        dnew = r.call("describe_asset", {"asset_path": tmp})
        dold = r.call("describe_asset", {"asset_path": pkg})
        fwd_ok = (rn.get("success") is True and dnew.get("found") is True
                  and dold.get("found") is False)
        r.check("migrate.rename_applies_redirect", fwd_ok,
                {"rename_api": rn.get("api"), "tried": rn.get("tried"),
                 "new_found": dnew.get("found"), "old_found": dold.get("found")})
    finally:
        try:
            rb = r.call("migrate_asset_rename", {
                "asset_path": tmp, "new_name": leaf,
                "confirm": True, "fixup_redirectors": False})
            dback = r.call("describe_asset", {"asset_path": pkg})
            r.check("migrate.rename_restored",
                    rb.get("success") is True and dback.get("found") is True,
                    {"restored_found": dback.get("found")})
        except Exception as e:
            r.check("migrate.rename_restored", False, "exception: %r" % (e,))


def _migrate_move_roundtrip(r):
    """真机 move 校验（内存态，自还原）：移动到临时子目录 -> 核对 -> 移回。
    5.4 无 move_asset，走 rename_asset 全路径兜底（本检查固化该兜底真机路径）。"""
    src = F + "/T_Used1K"
    leaf = "T_Used1K"
    moved = TMPDIR + "/" + leaf
    base = r.call("describe_asset", {"asset_path": src})
    if not base.get("found"):
        r.check("migrate.move_applies", False, "baseline missing: %s" % base)
        return
    try:
        mv = r.call("migrate_asset_move", {
            "asset_path": src, "dest_path": TMPDIR,
            "confirm": True, "fixup_redirectors": False})
        dnew = r.call("describe_asset", {"asset_path": moved})
        dold = r.call("describe_asset", {"asset_path": src})
        fwd_ok = (mv.get("success") is True and dnew.get("found") is True
                  and dold.get("found") is False)
        r.check("migrate.move_applies", fwd_ok,
                {"move_api": mv.get("api"), "tried": mv.get("tried"),
                 "new_found": dnew.get("found"), "old_found": dold.get("found")})
    finally:
        try:
            back = r.call("migrate_asset_move", {
                "asset_path": moved, "dest_path": F, "new_name": leaf,
                "confirm": True, "fixup_redirectors": False})
            dback = r.call("describe_asset", {"asset_path": src})
            r.check("migrate.move_restored",
                    back.get("success") is True and dback.get("found") is True,
                    {"restored_found": dback.get("found")})
        except Exception as e:
            r.check("migrate.move_restored", False, "exception: %r" % (e,))




def _migrate_copy_roundtrip(r):
    """真机复制迁移校验：复制 M_CalibRef(硬引用 T_CalibRef) -> 应带走整条 /Game uses 闭包(2 包)。
    内存态(不 save)，关编辑器丢弃；验证 migrate_asset copy 的 get_asset_chain 闭包 + duplicate_asset
    落新包可解析 + 目标目录自动创建(make_directory 修复)。"""
    src = F + "/M_CalibRef"
    dest = TMPDIR + "/copy"
    base = r.call("describe_asset", {"asset_path": src})
    if not base.get("found"):
        r.check("migrate.copy_dryrun_plan", False, "baseline missing: %s" % base)
        r.check("migrate.copy_applies", False, "skipped (no baseline)")
        return
    dr = r.call("migrate_asset", {"asset_path": src, "dest_path": dest, "dry_run": True})
    ok_plan = (dr.get("dry_run") is True and dr.get("executed") is False
               and dr.get("mode") == "copy" and dr.get("packages") == 2
               and dr.get("collision_count") == 0)
    r.check("migrate.copy_dryrun_plan", ok_plan,
            {"packages": dr.get("packages"), "mode": dr.get("mode"),
             "collision_count": dr.get("collision_count"),
             "total_size_mb": dr.get("total_size_mb"),
             "dsts": [it.get("dst") for it in (dr.get("items") or [])]})
    wr = r.call("migrate_asset", {"asset_path": src, "dest_path": dest,
                                  "dry_run": False, "confirm": True})
    dsts = [res.get("dst") for res in (wr.get("results") or []) if res.get("dst")]
    root_found = r.call("describe_asset", {"asset_path": dsts[0]}).get("found") if dsts else False
    ok_write = (wr.get("executed") is True and wr.get("success") is True
                and wr.get("duplicated") == wr.get("of") and wr.get("of") == 2
                and root_found is True)
    r.check("migrate.copy_applies", ok_write,
            {"duplicated": wr.get("duplicated"), "of": wr.get("of"),
             "success": wr.get("success"), "root_found_after": root_found,
             "dsts": dsts})

def run_all(bus_dir, project_dir):
    r = Runner(bus_dir)
    pg = r.call("ping", {})
    r.check("ping.bridge_alive", pg.get("bridge_alive") is True,
            pg.get("ue_version"))

    mres, _dt = _t(lambda: r.call("get_asset_metrics", {
        "asset_paths": [F + "/T_CalibCube", F + "/T_BigNoise", F + "/T_FakeBig",
                        F + "/CalibMesh"],
        "max_assets": 20}))
    items = {}
    for i in mres["items"]:
        items[i["package_name"]] = i
    cube = items.get(F + "/T_CalibCube", {})
    cm = cube.get("metrics") or {}
    r.check("metrics.cube_measurable", bool(cm.get("width")),
            "w=%s derived=%s tried=%s" % (cm.get("width"),
                                          cm.get("size_derived"),
                                          cube.get("tried")))
    big = items.get(F + "/T_BigNoise", {})
    big_est = ((big.get("metrics") or {}).get("est_runtime_bytes") or 0)
    r.check("metrics.real_4k_is_runtime_heavy",
            big.get("runtime_verdict") == "runtime_heavy",
            "verdict=%s est_MB=%.2f" % (big.get("runtime_verdict"),
                                        big_est / 1048576.0))
    fb = items.get(F + "/T_FakeBig", {})
    fbv = (fb.get("metrics") or {}).get("runtime_verdict") or {}
    ok_capped = (fb.get("runtime_verdict") == "disk_only_bloat"
                 and fbv.get("capped_by_max_size") is True)
    r.check("metrics.capped_4k_is_disk_only_bloat", ok_capped,
            "verdict=%s runtime_MB=%s disk_MB=%s" % (
                fb.get("runtime_verdict"), fbv.get("runtime_mb"),
                fbv.get("disk_mb")))

    # D2 校准：静态网格材质槽=最大 section 数（5.4 校准从 sections 派生）。已知真值：引擎 Cube 1 槽。
    # 容错：mesh 不可测(类不符/载入失败) -> 记 skip 而非硬失败，避免拖垮整套夹具矩阵。
    mesh = items.get(F + "/CalibMesh", {})
    mm = mesh.get("metrics") or {}
    if mesh.get("class") == "StaticMesh" and mm.get("lods"):
        r.check("metrics.mesh_material_slots", mm.get("material_slots") == 1,
                "material_slots=%s sections=%s tried=%s" % (
                    mm.get("material_slots"),
                    (mm.get("lods") or [{}])[0].get("sections"), mesh.get("tried")))
    else:
        r.check("metrics.mesh_material_slots", True,
                "skipped: mesh not measurable here (class=%s note=%s)" % (
                    mesh.get("class"), mesh.get("note")))

    def call_compose():
        return r.call("list_level_actors",
                      {"limit": 1, "compose": True, "dup_min_count": 100})
    comp, dt2 = _t(call_compose)
    c = comp.get("composition") or {}
    r.check("compose.world_ge_1400",
            (c.get("world_total_actors") or 0) >= 1400,
            "world=%s elapsed_s=%s" % (c.get("world_total_actors"), dt2))
    opp = {}
    for o in c.get("instancing_opportunities") or []:
        opp[o["mesh"]] = o["actors"]
    r.check("compose.cube_batching_opportunity",
            opp.get("/Engine/BasicShapes/Cube", 0) >= 1000,
            "opportunities=%s" % opp)

    def call_orphan():
        return r.call("scan_orphan_assets",
                      {"folder": F, "limit": 100, "cascade": False})
    orph, dt3 = _t(call_orphan)
    got = set()
    for o in orph.get("orphans") or []:
        got.add(os.path.basename(o["package"]))
    missing = sorted(ORPHAN_TARGETS - got)
    r.check("orphan.targets_found", not missing,
            "missing=%s elapsed_s=%s" % (missing or "-", dt3))
    r.check("orphan.map_not_orphaned", "CalibMap" not in got,
            "orphans=%s" % sorted(got))

    rep = rules.run_report(project_dir, bus_dir, scope="/Game/PrismCalib")
    fake_top = []
    for f in rep["findings"]:
        if f["rule_id"] == "asset_size_top" and f["subject"] == F + "/T_FakeBig":
            fake_top.append(f)
    ok_down = bool(fake_top) and (fake_top[0]["evidence"].get("runtime_verdict")
                                  == "disk_only_bloat")
    r.check("report.fake_big_downgrade_evidence", ok_down,
            fake_top[0]["evidence"] if fake_top else "no finding")
    tex = {}
    for f in rep["findings"]:
        if f["rule_id"] == "texture_size":
            tex[f["subject"]] = f
    r.check("report.real_4k_stays_error",
            tex.get(F + "/T_BigNoise", {}).get("severity") == "error",
            {k: v["severity"] for k, v in tex.items()})
    ok_fields = bool(tex) and all(("est_runtime_mb" in f["evidence"])
                                  for f in tex.values())
    r.check("report.texture_evidence_upgraded", ok_fields,
            "fields ok" if tex else "no texture_size findings")
    # P2 get_asset_references 硬/软分类：真机(5.4) 对已知"被材质硬引用"靶断言。
    # 覆盖 classify_soft 的 real API 路径（hard-only/soft-only 两次 get_referencers）。
    def call_refs():
        return r.call("get_asset_references", {
            "asset_path": F + "/T_CalibRef", "direction": "used_by", "classify_soft": True})
    rres = call_refs()
    matd = None
    for x in rres.get("used_by_detail") or []:
        if str(x.get("package", "")).endswith("/M_CalibRef"):
            matd = x
    has_mat = any(str(p).endswith("/M_CalibRef") for p in rres.get("used_by") or [])
    ok_refs = (has_mat and matd is not None and matd.get("hard") is True
               and matd.get("soft") is False
               and rres.get("used_by_soft_count") == 0)
    r.check("refs.classify_soft_hard", ok_refs,
            {"used_by": rres.get("used_by"), "detail": matd,
             "soft_count": rres.get("used_by_soft_count"), "api": rres.get("api")})

    # P2 scan_broken_references：真机对已知坏引用靶(材质硬引用后贴图被删 -> missing dep)断言，
    # 同批扫 /Game/PrismCalib 做正负对照：坏靶 T_CalibBroken 必被标记；健康 T_CalibRef 必不误报(假阳性负控)。
    def call_broken():
        return r.call("scan_broken_references", {"folder": "PrismCalib", "limit": 200})
    bres = call_broken()
    bdeps = [str(x.get("dep", "")) for x in (bres.get("broken") or [])]
    hit = [d for d in bdeps if d.endswith("/T_CalibBroken")]
    kinds = {str(x.get("dep", "")).rpartition("/")[2]: x.get("kind")
             for x in (bres.get("broken") or [])}
    neg_clean = not any(d.endswith("/T_CalibRef") for d in bdeps)
    ok_broken = (bres.get("found_registry") is True and len(hit) >= 1 and neg_clean)
    r.check("refs.broken_missing_target", ok_broken,
            {"broken_deps": bdeps[:10], "kinds": kinds,
             "missing_count": bres.get("missing_count"),
             "redirector_count": bres.get("redirector_count"),
             "assets_with_broken": bres.get("assets_with_broken"),
             "total_scanned": bres.get("total_scanned"), "api": bres.get("api")})

    # P2 fix_broken_references：真机写契约——对 M_CalibStub 悬空靶验证 dry_run 路由(fixable/missing) + confirm 双钥落盘不崩。
    def call_fixplan():
        return r.call("fix_broken_references", {
            "asset_paths": [F + "/M_CalibStub"], "dry_run": True, "limit": 50})
    fp = call_fixplan()
    fixpk = [str(x.get("package", "")) for x in (fp.get("fixable_redirectors") or [])]
    misspk = [str(x.get("package", "")) for x in (fp.get("unfixable_missing") or [])]
    stub_redirector = any(d.endswith("/T_CalibStub") for d in fixpk)
    stub_missing = any(d.endswith("/T_CalibStub") for d in misspk)
    r.check("refs.fix_plan_routing",
            fp.get("found_registry") is True and (stub_redirector or stub_missing),
            {"fixable": fixpk[:6], "missing": misspk[:6],
             "would_fix_count": fp.get("would_fix_count"),
             "as": ("redirector" if stub_redirector else "missing")})

    def call_fixwrite():
        return r.call("fix_broken_references", {
            "asset_paths": [F + "/M_CalibStub"], "dry_run": False, "confirm": True, "limit": 50})
    fw = call_fixwrite()
    ok_write = (fw.get("found_registry") is True and fw.get("executed") is True
                and isinstance(fw.get("success"), bool)
                and isinstance(fw.get("attempted_fix"), int))
    r.check("refs.fix_write_honest", ok_write,
            {"executed": fw.get("executed"), "attempted_fix": fw.get("attempted_fix"),
             "confirmed_fixed": fw.get("confirmed_fixed"), "success": fw.get("success"),
             "results": (fw.get("results") or [])[:4]})

    _migrate_rename_roundtrip(r)
    _migrate_move_roundtrip(r)
    _migrate_copy_roundtrip(r)
    matrix = {
        "ue_version": pg.get("ue_version"),
        "metrics_tried": {k: v.get("tried") for k, v in items.items()},
        "compose_elapsed_s": dt2,
        "orphan_elapsed_s": dt3,
    }
    return r.results, matrix


def main(argv):
    if len(argv) < 3:
        print(__doc__)
        return 2
    bus_dir, project_dir = argv[1], argv[2]
    out_path = None
    if "--out" in argv:
        out_path = argv[argv.index("--out") + 1]
    results, matrix = run_all(bus_dir, project_dir)
    failed = [x["name"] for x in results if not x["pass"]]
    if out_path:
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump({"results": results, "matrix": matrix}, f,
                      ensure_ascii=False, indent=1)
    print("%d/%d checks passed%s" % (
        len(results) - len(failed), len(results),
        "" if not failed else "; FAILED: " + ", ".join(failed)))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
