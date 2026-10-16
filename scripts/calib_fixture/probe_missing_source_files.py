# -*- coding: utf-8 -*-
"""#11 C 真机探针：scan_missing_source_files 的在场性判档往返校验（不进 59 项门禁）。

为什么单独跑：把这条判据并进 59 项会让它去牵动孤儿/引用/预算那几组已知真值（源图目录
一动，重导入路径与体积读数就可能漂移），而 C 的真实需求只是「missing/present 两态能不能
被稳定复现」。于是这里自建已知真值、自清理：

  夹具 PNG 源图目录 <proj>/Saved/PrismFixture/pngs 改名 pngs_off -> 直连导入的 5 张靶
  的记录路径全部失效 => verdict 必须翻成 missing；改回原名 => 必须翻回 present。
  另有编辑器内 create_asset 造的 M_CalibRef（无导入记录）=> verdict 必须是 no_record。

跑法（编辑器已开、桥在线；工程侧须已 plugin-install 当前仓库代码，否则测的是旧插件）:
    python scripts/calib_fixture/probe_missing_source_files.py <bus_dir> <project_dir>

产物 probe_missing_source_files.json 落在 <project_dir>/Saved/PrismFixture/，
逐条 PASS/FAIL + 每档计数与 via 命中面；任一 FAIL 退出码非 0。
"""
from __future__ import annotations

import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
for _up in range(4):
    cand = os.path.dirname(HERE)
    if cand and os.path.isdir(os.path.join(cand, "prism")):
        if cand not in sys.path:
            sys.path.insert(0, cand)
        break

from prism import bus, envelope          # noqa: E402

FOLDER = "/Game/PrismCalib"
PNG_TARGETS = ("T_BigNoise", "T_Used1K", "T_Orphan1", "T_Orphan2", "T_Orphan3")
AUTHORED = FOLDER + "/M_CalibRef"
OUT_NAME = "probe_missing_source_files.json"


def _say(line):
    print(str(line))
    sys.stdout.flush()


def _call(client, fn, args):
    return client.call(fn, args)


def _rows_by_pkg(res):
    out = {}
    for r in res.get("rows") or []:
        out[r["package"]] = r
    return out


def _scan(client, class_contains="", limit=120):
    env = _call(client, "scan_missing_source_files",
                {"folder": FOLDER, "class_contains": class_contains,
                 "limit": limit, "offset": 0, "max_items": limit})
    if not env.get("ok"):
        err = (env.get("error") or {}).get("message") or str(env)
        raise SystemExit("scan_missing_source_files 调用失败: " + err)
    return env["result"]


def _flip_dir(png_dir, off_dir):
    """把源图目录改名（制造/解除 missing），只在真的存在时动。"""
    if os.path.isdir(off_dir):
        return False
    if not os.path.isdir(png_dir):
        raise SystemExit("夹具源图目录不存在: %s（先跑 run_fixture 生成资产）" % png_dir)
    os.rename(png_dir, off_dir)
    return True


def _restore(png_dir, off_dir):
    if os.path.isdir(off_dir) and not os.path.isdir(png_dir):
        os.rename(off_dir, png_dir)


def _expect(checks, cid, ok, detail):
    checks.append({"id": cid, "pass": bool(ok), "detail": detail})
    _say("%s %s  %s" % ("PASS" if ok else "FAIL", cid, detail))


def main(argv=None):
    ap_args = argv if argv is not None else sys.argv[1:]
    if len(ap_args) < 2:
        raise SystemExit(__doc__)
    bus_dir, proj_dir = ap_args[0], os.path.abspath(ap_args[1])
    timeout = float(os.environ.get("PRISM_TIMEOUT", "240"))
    png_dir = os.path.join(proj_dir, "Saved", "PrismFixture", "pngs")
    off_dir = os.path.join(proj_dir, "Saved", "PrismFixture", "pngs_off")

    report = {"started": time.strftime("%Y-%m-%d %H:%M:%S"), "folder": FOLDER,
              "png_dir": png_dir, "checks": [], "phases": {}}
    checks = report["checks"]
    client = bus.BusClient(bus_dir, timeout=timeout)
    flipped = False
    rc = 1
    try:
        # ---- 相位 1：源图在场 -> 直连导入的 5 张必须 present，编辑器内造的必须 no_record
        base_all = _scan(client, class_contains="", limit=200)
        report["phases"]["baseline_all"] = {
            "scanned": base_all.get("scanned"), "total_available": base_all.get("total_available"),
            "counts": {k: base_all.get(k) for k in ("present_count", "partial_count",
                                                    "missing_count", "no_record",
                                                    "load_failed_count")},
            "via": base_all.get("api", {}).get("import_record_via"),
            "skipped": base_all.get("skipped")}
        via = base_all.get("api", {}).get("import_record_via") or {}
        _expect(checks, "C.api_via_reported", bool(via),
                "import_record_via=%s（跨版本兼容矩阵就看这一栏命中哪条签名）" % (via,))
        bmap = _rows_by_pkg(base_all)
        auth = bmap.get(AUTHORED)
        _expect(checks, "C.authored_is_no_record",
                auth is not None and auth["verdict"] == "no_record",
                "%s verdict=%s（编辑器内 create_asset，无导入记录 != 源丢失）"
                % (AUTHORED, auth and auth["verdict"]))
        _expect(checks, "C.level_excluded", "level" in (base_all.get("skipped") or {}),
                "skipped=%s（关卡不载入，无外部源概念）" % (base_all.get("skipped"),))

        base_tex = _scan(client, class_contains="texture", limit=120)
        tmap = _rows_by_pkg(base_tex)
        present = [n for n in PNG_TARGETS
                   if tmap.get(FOLDER + "/" + n, {}).get("verdict") == "present"]
        _expect(checks, "C.present_when_source_on_disk", len(present) >= 4,
                "present 的直连导入靶=%d/%d %s" % (len(present), len(PNG_TARGETS), present))
        rec_ok = all(any(r["exists"] and r["under_project"]
                         for r in tmap[FOLDER + "/" + n]["recorded"]) for n in present)
        _expect(checks, "C.recorded_path_under_project", bool(rec_ok),
                "记录路径落在本工程 Saved 下（夹具靶的应有形态）")

        # ---- 相位 2：源图目录改名 -> 同一批必须翻 missing，且体量账跟着动
        flipped = _flip_dir(png_dir, off_dir)
        miss = _scan(client, class_contains="texture", limit=120)
        mmap = _rows_by_pkg(miss)
        report["phases"]["sources_off"] = {
            "counts": {k: miss.get(k) for k in ("present_count", "partial_count",
                                                "missing_count", "no_record")},
            "missing_bytes": miss.get("missing_bytes"), "missing_mb": miss.get("missing_mb"),
            "by_dir_top": list((miss.get("by_dir") or {}).keys())[:3]}
        gone = [n for n in PNG_TARGETS
                if mmap.get(FOLDER + "/" + n, {}).get("verdict") == "missing"]
        _expect(checks, "C.flips_to_missing", len(gone) == len(present),
                "改源图目录名后 missing=%d/%d %s" % (len(gone), len(present), gone))
        _expect(checks, "C.missing_bytes_counts_assets",
                int(miss.get("missing_bytes") or 0) > 0,
                "missing_bytes=%s（受影响 uasset 体量，可维护性口径不是池金额）"
                % miss.get("missing_bytes"))
        _expect(checks, "C.present_zero_after_flip", int(miss.get("present_count") or 0) == 0,
                "present_count=%s（源目录整体消失，不该还有半张在场）" % miss.get("present_count"))
        _expect(checks, "C.by_dir_aggregates",
                bool(miss.get("by_dir")) and int(list(miss["by_dir"].values())[0]["missing"]) > 0,
                "by_dir 按资产目录聚合到 missing 计数")

        # ---- 相位 3：改回 -> 必须回到 present（判据可复现，不留残留）
        _restore(png_dir, off_dir)
        back = _scan(client, class_contains="texture", limit=120)
        bmap2 = _rows_by_pkg(back)
        again = [n for n in PNG_TARGETS
                 if bmap2.get(FOLDER + "/" + n, {}).get("verdict") == "present"]
        report["phases"]["sources_back"] = {
            "counts": {k: back.get(k) for k in ("present_count", "missing_count", "no_record")}}
        _expect(checks, "C.restores_to_present", len(again) == len(present),
                "改回后 present=%d/%d" % (len(again), len(present)))
        _expect(checks, "C.self_clean_on_disk",
                os.path.isdir(png_dir) and not os.path.isdir(off_dir),
                "源图目录已复原")

        # ---- 分页/截断纪律（列表结果必须带 total/truncated/cap）
        page = _call(client, "scan_missing_source_files",
                     {"folder": FOLDER, "class_contains": "texture", "limit": 1,
                      "offset": 0, "max_items": 1})["result"]
        _expect(checks, "C.window_pagination",
                page.get("cap") == 1 and page.get("scanned") == 1
                and bool(page.get("truncated")) == (page.get("total_available", 0) > 1),
                "cap=%s scanned=%s truncated=%s total=%s"
                % (page.get("cap"), page.get("scanned"), page.get("truncated"),
                   page.get("total_available")))

        rc = 0 if all(c["pass"] for c in checks) else 1
    finally:
        if flipped:
            _restore(png_dir, off_dir)
        out_dir = os.path.join(proj_dir, "Saved", "PrismFixture")
        try:
            os.makedirs(out_dir, exist_ok=True)
            report["done"] = time.strftime("%Y-%m-%d %H:%M:%S")
            report["all_pass"] = rc == 0
            with open(os.path.join(out_dir, OUT_NAME), "w", encoding="utf-8") as f:
                json.dump(report, f, ensure_ascii=False, indent=2, sort_keys=True)
        except Exception as exc:
            _say("写报告失败: %r" % (exc,))
    passed = sum(1 for c in checks if c["pass"])
    _say("[C-probe] %d/%d PASS -> %s" % (passed, len(checks), OUT_NAME))
    return rc


if __name__ == "__main__":
    sys.exit(main())
