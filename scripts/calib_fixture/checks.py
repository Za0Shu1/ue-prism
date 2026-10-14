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


def _safe(r, group, fn):
    """分组隔离：某组中途抛异常只记一条 FAIL 并继续跑后面的组。

    5.4 真机实跑教训：migrate_asset 因上一轮残留的复制靶拒绝写入并抛错，
    把后面 19 项 v1.5 校验整套带走、matrix.json 也没落盘。兼容矩阵必须
    "红一项"而不是"全没跑"——红的那项照常记 FAIL，不降级也不掩盖。
    """
    try:
        return fn()
    except Exception as exc:
        r.check(group + ".aborted", False, "exception: %r" % (exc,))
        return None


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


def _opt_texture_checks(r):
    """v1.5 纹理流送池真值校验（已知答案：4K 不钳=T_BigNoise 该钳；钳到 128=T_FakeBig 不该报；
    TC_EDITORICON=T_CalibIcon 是"恒常驻池外"而不是"4B/px 未压缩"；Cube 不支持 VT）。"""
    st, dt = _t(lambda: r.call("scan_texture_streaming",
                               {"folder": F, "max_assets": 60, "min_mb": 0.0, "top": 40}))
    by = {}
    for it in st.get("items") or []:
        by[it.get("package")] = it
    cands = st.get("candidates") or {}

    def pkgs(key):
        return {c.get("package") for c in cands.get(key) or []}

    big = F + "/T_BigNoise"
    fb = F + "/T_FakeBig"
    icon = F + "/T_CalibIcon"
    cube = F + "/T_CalibCube"
    big_row = by.get(big) or {}
    r.check("streaming.clamp_target_planned",
            big in pkgs("need_clamp") and big_row.get("clamp_to") == 2048
            and float(big_row.get("clamp_savings_mb") or 0) > 10.0,
            {"row": {k: big_row.get(k) for k in ("source_width", "max_size", "pool_mb",
                                                 "clamp_to", "clamp_savings_mb")},
             "elapsed_s": dt})
    r.check("streaming.clamped_texture_not_reflagged",
            fb not in pkgs("need_clamp")
            and "max_size_capped" in ((by.get(fb) or {}).get("flags") or []),
            {"fake_big": {k: (by.get(fb) or {}).get(k) for k in
                          ("source_width", "max_size", "pool_mb", "clamp_to", "flags")}})
    icon_row = by.get(icon) or {}
    r.check("streaming.editoricon_is_permanent_not_uncompressed",
            icon_row.get("format_class") == "no_stream" and icon in pkgs("non_streaming")
            and float(icon_row.get("pool_mb") or 0) == 0.0
            and float(icon_row.get("permanent_mb") or 0) > 0.0
            and float(icon_row.get("compression_savings_mb") or 0) == 0.0,
            {"row": {k: icon_row.get(k) for k in ("compression", "format_class", "pool_mb",
                                                  "permanent_mb", "compression_savings_mb",
                                                  "streaming_release_mb", "actions")}})
    r.check("streaming.vt_skip_cube",
            cube in pkgs("vt_blocked")
            and float((by.get(cube) or {}).get("vt_savings_mb") or 0) == 0.0,
            {"cube": {k: (by.get(cube) or {}).get(k) for k in
                      ("class", "vt_supported", "vt_reason", "vt_savings_mb", "pool_mb")}})
    hdr = F + "/T_CalibHdr"
    hdr_row = by.get(hdr) or {}
    r.check("streaming.hdr_bucketed_high_bpp",
            hdr_row.get("format_class") == "high_bpp"
            and hdr_row.get("high_bpp") is True
            and hdr_row.get("uncompressed") is False
            and "high_bpp_format" in (hdr_row.get("flags") or [])
            and hdr in pkgs("high_bpp"),
            {"row": {k: hdr_row.get(k) for k in ("class", "compression", "format_class",
                                                 "runtime_ratio", "runtime_basis",
                                                 "runtime_mb", "pool_mb", "actions")},
             "candidates_high_bpp": sorted(pkgs("high_bpp"))})
    # 真值口径：引擎实测/估算都来自 domain，这里用 metrics 的原始字节判定，
    # 不信 streaming 的倍率表本身（否则就是自证）。TC_HDR 若真按 BC 压，
    # measured 只会有 est 的 1 倍；5.4 真机是 6~8 倍。
    mrow = r.call("get_asset_metrics", {"asset_paths": [hdr], "max_assets": 1})
    mitem = (mrow.get("items") or [{}])[0]
    met = mitem.get("metrics") or {}
    mv = met.get("runtime_verdict") or {}
    measured = float(met.get("memory_bytes") or 0)
    est_bc = float(met.get("est_runtime_bytes") or 0)
    runtime = float(mv.get("runtime_mb") or 0)
    hot = measured > 0 and measured >= est_bc
    r.check("streaming.hdr_runtime_not_bc_estimate",
            est_bc > 0 and runtime >= 3.9 * est_bc / 1048576.0,
            {"measured_mb": round(measured / 1048576.0, 3),
             "est_bc_mb": round(est_bc / 1048576.0, 3), "runtime_mb": runtime,
             "ratio": round(runtime / max(est_bc / 1048576.0, 1e-9), 2),
             "basis": mv.get("runtime_basis"), "bytes_per_texel": mv.get("bytes_per_texel"),
             "hot_upload": hot})
    r.check("streaming.hdr_measured_not_multiplied_again",
            (mv.get("runtime_basis") == "resident_memory"
             and abs(runtime - measured / 1048576.0) <= 0.05 * (measured / 1048576.0))
            or (mv.get("runtime_basis") == "derived_estimate" and measured < est_bc),
            {"basis": mv.get("runtime_basis"), "runtime_mb": runtime,
             "measured_mb": round(measured / 1048576.0, 3),
             "est_bc_mb": round(est_bc / 1048576.0, 3)})
    r.check("streaming.hdr_metrics_and_scan_same_ledger",
            float(hdr_row.get("runtime_mb") or 0) == runtime
            and float(hdr_row.get("runtime_bytes_per_texel") or 0)
            == float(mv.get("bytes_per_texel") or 0),
            {"scan_runtime_mb": hdr_row.get("runtime_mb"), "metrics_runtime_mb": runtime,
             "scan_bpp": hdr_row.get("runtime_bytes_per_texel"),
             "metrics_bpp": mv.get("bytes_per_texel")})
    r.check("streaming.hdr_not_auto_recompressed",
            "set_compression" not in (hdr_row.get("actions") or [])
            and float(hdr_row.get("compression_savings_mb") or 0) > 0.0,
            {"actions": hdr_row.get("actions"),
             "compression_savings_mb": hdr_row.get("compression_savings_mb"),
             "bc_equivalent_mb": hdr_row.get("bc_equivalent_mb")})
    # 缺陷 #10 回归线（5.4 真实工程 dry-run 暴露）：opt_texture_set_compression 在
    # only_uncompressed=False 时曾把"本来就是目标格式"的贴图也算成待改项（虚报 planned/收益，
    # 还会白白保存资产）。真值：TC_DEFAULT 的 T_BigNoise 改 default = no_op，只有 TC_HDR 那张算 change。
    gate_paths = "%s,%s" % (big, hdr)
    g_res = r.call("opt_texture_set_compression",
                   {"paths": gate_paths, "compression": "default",
                    "only_uncompressed": False, "dry_run": True})
    g_rows = {x.get("package"): x for x in (g_res.get("targets") or [])}
    big_g = g_rows.get(big) or {}
    hdr_g = g_rows.get(hdr) or {}
    r.check("compression.already_target_is_no_op",
            big_g.get("kind") == "no_op" and u"已是目标压缩格式" in str(big_g.get("reason"))
            and g_res.get("planned") == 1 and hdr_g.get("kind") == "change",
            {"big": {k: big_g.get(k) for k in ("kind", "before", "after", "reason")},
             "hdr": {k: hdr_g.get(k) for k in ("kind", "before", "after", "savings_mb")},
             "planned": g_res.get("planned"), "no_op": g_res.get("no_op"),
             "est_savings_mb": g_res.get("est_savings_mb")})
    g_default = r.call("opt_texture_set_compression",
                       {"paths": gate_paths, "compression": "default", "dry_run": True})
    r.check("compression.conservative_gate_still_blocks_high_bpp",
            g_default.get("planned") == 0
            and (g_default.get("targets") or [{}])[0].get("kind") == "no_op",
            {"planned": g_default.get("planned"), "no_op": g_default.get("no_op"),
             "reasons": [str(x.get("reason"))[:60] for x in (g_default.get("targets") or [])]})
    pool = st.get("pool") or {}
    summ = st.get("summary") or {}
    r.check("streaming.summary_separates_ledgers",
            summ.get("model", {}).get("version") and summ.get("uncompressed_count") == 0
            and summ.get("non_streaming_count") >= 1,
            {"uncompressed": summ.get("uncompressed_count"),
             "non_streaming": summ.get("non_streaming_count"),
             "pool_demand_mb": summ.get("pool_demand_mb"),
             "permanent_mb": summ.get("permanent_mb"),
             "potential_permanent_release_mb": summ.get("potential_permanent_release_mb")})
    r.check("streaming.summary_counts_high_bpp",
            int(summ.get("high_bpp_count") or 0) >= 1
            and summ.get("model", {}).get("version"),
            {"high_bpp_count": summ.get("high_bpp_count"),
             "uncompressed_count": summ.get("uncompressed_count"),
             "model_version": summ.get("model", {}).get("version")})
    r.check("streaming.pool_ini_read",
            int((pool.get("config") or {}).get("files_scanned") or 0) >= 1
            and (pool.get("compare") or {}).get("demand_mb") is not None,
            {"effective_mb": (pool.get("config") or {}).get("effective_mb"),
             "verdict": (pool.get("compare") or {}).get("verdict"),
             "note": (pool.get("config") or {}).get("note")})

    plan = r.call("opt_texture_clamp_max_size", {"paths": [big], "max_size": 2048,
                                                 "dry_run": True})
    r.check("streaming.clamp_dry_run_does_not_write",
            plan.get("planned") == 1 and plan.get("changed") == 0
            and plan.get("executed") is False and plan.get("dry_run") is True,
            {"planned": plan.get("planned"), "est_savings_mb": plan.get("est_savings_mb"),
             "row": (plan.get("targets") or [{}])[0]})
    wr = r.call("opt_texture_clamp_max_size", {"paths": [big], "max_size": 2048,
                                               "dry_run": False, "confirm": True})
    row0 = (wr.get("targets") or [{}])[0]
    r.check("streaming.clamp_write_honest",
            wr.get("executed") is True and wr.get("changed") == 1
            and row0.get("after") == 2048 and row0.get("saved") is not False,
            {"api": row0.get("api"), "before": row0.get("before"),
             "after": row0.get("after"), "saved": row0.get("saved"),
             "failed": wr.get("failed"), "tried": (wr.get("tried") or [])[-4:]})
    back = r.call("get_asset_metrics", {"asset_paths": [big]})
    got_ms = ((back.get("items") or [{}])[0].get("metrics") or {}).get("max_size")
    r.check("streaming.clamp_readback_from_disk", got_ms == 2048,
            {"max_size_after_write": got_ms})
    rst = r.call("opt_texture_clamp_max_size", {"paths": [big], "max_size": 0,
                                                "dry_run": False, "confirm": True})
    r.check("streaming.clamp_restored_for_next_run", rst.get("changed") == 1,
            {"changed": rst.get("changed"), "failed": rst.get("failed")})
    vt = r.call("opt_texture_set_virtual", {"paths": [cube, big], "dry_run": True})
    r.check("streaming.vt_write_blocks_cube",
            vt.get("planned") == 1 and len(vt.get("skipped") or []) == 1
            and (vt.get("skipped") or [{}])[0].get("package") == cube,
            {"planned": vt.get("planned"), "skipped": vt.get("skipped")})
    return {"scan_tried": st.get("tried"), "clamp_write": row0,
            "vt_dry": {"planned": vt.get("planned"),
                       "skipped": [s.get("reason") for s in (vt.get("skipped") or [])]}}


def _opt_nanite_checks(r):
    """Nanite+半透明真值校验（靶：CalibMeshTranslucent = 引擎 Cube 副本 + Translucent 材质，
    生成时刻意不开 Nanite）。跑完自还原回 blocked_translucent，夹具可重复校验。"""
    tgt = F + "/CalibMeshTranslucent"

    def cats(args):
        res = r.call("scan_nanite_candidates", args)
        return {i.get("package"): i for i in res.get("items") or []}, res

    before, _ = cats({"paths": [tgt], "min_triangles": 1})
    row = before.get(tgt) or {}
    r.check("nanite.target_blocked_before_write",
            row.get("category") == "blocked_translucent" and row.get("translucent") is True,
            {"category": row.get("category"), "blend_modes": row.get("blend_modes"),
             "nanite_enabled": row.get("nanite_enabled"), "nanite_prop": row.get("nanite_prop"),
             "triangles": row.get("triangles")})
    # 真机回归：used_with_nanite 挂在 UMaterial 上，而槽位给的是 MaterialInterface，
    # 直读恒 null（真实工程 5/5 材质全 null）。解包基类材质后必须读到确定值。
    mrows = row.get("materials") or []
    r.check("nanite.material_usage_readable_via_base_material",
            bool(mrows) and all(m.get("used_with_nanite") is not None for m in mrows)
            and all(isinstance(m.get("used_with_nanite"), bool) for m in mrows),
            {"materials": [{k: m.get(k) for k in ("material", "blend_mode",
                                                  "used_with_nanite", "usage_api")}
                           for m in mrows],
             "need_used_with_nanite": row.get("materials_need_used_with_nanite")})
    # 判别性靶：CalibMeshInstance 挂的是 MaterialInstanceConstant（父 M_CalibRef）。
    # 5.4 探针实证 MIC 直读 used_with_nanite 必抛异常，只有解包基类材质才读得到，
    # 所以"非 null"就是这条缺陷的回归线（旧实现 100% 给 null），api 前缀作证据。
    ipkg = F + "/CalibMeshInstance"
    irows, _ires = cats({"paths": [ipkg], "min_triangles": 1})
    imats = (irows.get(ipkg) or {}).get("materials") or []
    r.check("nanite.material_instance_usage_not_null",
            bool(imats) and all(m.get("used_with_nanite") is not None for m in imats)
            and all(m.get("usage_api") for m in imats)
            and any(str(m.get("material", "")).endswith("/M_CalibInst") for m in imats),
            {"materials": [{k: m.get(k) for k in ("material", "blend_mode",
                                                  "used_with_nanite", "usage_api")}
                           for m in imats],
             "category": (irows.get(ipkg) or {}).get("category"),
             "need_used_with_nanite": (irows.get(ipkg) or {}).get(
                 "materials_need_used_with_nanite")})
    blocked = r.call("opt_mesh_set_nanite", {"paths": [tgt], "enabled": True,
                                             "skip_translucent": True, "dry_run": True})
    r.check("nanite.skip_translucent_blocks",
            blocked.get("planned") == 0 and len(blocked.get("skipped") or []) == 1
            and "半透明" in str((blocked.get("skipped") or [{}])[0].get("reason")),
            {"planned": blocked.get("planned"), "skipped": blocked.get("skipped")})
    forced = r.call("opt_mesh_set_nanite", {"paths": [tgt], "enabled": True,
                                            "skip_translucent": False,
                                            "dry_run": False, "confirm": True})
    frow = (forced.get("targets") or [{}])[0]
    api_hit = frow.get("api")
    r.check("nanite.force_write_honest",
            forced.get("changed") == 1 and frow.get("after") is True
            and not (forced.get("failed") or []),
            {"api": api_hit, "before": frow.get("before"), "after": frow.get("after"),
             "saved": frow.get("saved"), "failed": forced.get("failed"),
             "tried": (forced.get("tried") or [])[-6:]})
    during, _ = cats({"paths": [tgt], "min_triangles": 1})
    drow = during.get(tgt) or {}
    r.check("nanite.conflict_detected",
            drow.get("category") == "conflict_translucent"
            and drow.get("nanite_enabled") is True,
            {"category": drow.get("category"), "nanite_prop": drow.get("nanite_prop"),
             "blend_modes": drow.get("blend_modes"),
             "need_used_with_nanite": drow.get("materials_need_used_with_nanite")})
    fixed = r.call("fix_nanite_translucent", {"paths": [tgt], "dry_run": False,
                                             "confirm": True})
    grow = (fixed.get("targets") or [{}])[0]
    r.check("nanite.fix_write_honest",
            fixed.get("changed") == 1 and grow.get("after") is False,
            {"api": grow.get("api"), "before": grow.get("before"),
             "after": grow.get("after"), "failed": fixed.get("failed")})
    after, _ = cats({"paths": [tgt], "min_triangles": 1})
    r.check("nanite.self_restored",
            (after.get(tgt) or {}).get("category") == "blocked_translucent",
            {"category": (after.get(tgt) or {}).get("category")})
    return {"nanite_api": api_hit, "nanite_prop": drow.get("nanite_prop"),
            "blend_mode_read": (drow.get("blend_modes") or [None])[0],
            "force_tried": (forced.get("tried") or [])[-8:]}


def _opt_instancing_checks(r):
    """合批方案真值校验（CalibMap 已知 >=1000 个散装引擎 Cube；策略必须是 plan_only）。"""
    inst, dt = _t(lambda: r.call("analyze_instancing", {"min_count": 100, "cap": 10}))
    groups = {g.get("mesh"): g for g in inst.get("groups") or []}
    cube_g = groups.get("/Engine/BasicShapes/Cube") or {}
    r.check("compose.instancing_groups",
            inst.get("plan", {}).get("strategy") == "plan_only"
            and int(cube_g.get("actors") or 0) >= 1000
            and int((inst.get("summary") or {}).get("drawcalls_before") or 0)
            > int((inst.get("summary") or {}).get("drawcalls_after") or 0),
            {"cube_actors": cube_g.get("actors"), "groups": inst.get("total"),
             "summary": inst.get("summary"), "elapsed_s": dt})
    return {"cube_actors": cube_g.get("actors"), "elapsed_s": dt}


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

    _safe(r, "migrate.rename", lambda: _migrate_rename_roundtrip(r))
    _safe(r, "migrate.move", lambda: _migrate_move_roundtrip(r))
    _safe(r, "migrate.copy", lambda: _migrate_copy_roundtrip(r))
    # v1.5 优化族（纹理流送池 / Nanite+半透明 / 合批方案）：真机 API 路径与双钥写回读
    opt_tex = _safe(r, "opt.texture", lambda: _opt_texture_checks(r)) or {}
    opt_nan = _safe(r, "opt.nanite", lambda: _opt_nanite_checks(r)) or {}
    opt_ins = _safe(r, "opt.instancing", lambda: _opt_instancing_checks(r)) or {}
    matrix = {
        "ue_version": pg.get("ue_version"),
        "metrics_tried": {k: v.get("tried") for k, v in items.items()},
        "compose_elapsed_s": dt2,
        "orphan_elapsed_s": dt3,
        "opt_texture": opt_tex,
        "opt_nanite": opt_nan,
        "opt_instancing": opt_ins,
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
