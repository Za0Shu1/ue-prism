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


def run_all(bus_dir, project_dir):
    r = Runner(bus_dir)
    pg = r.call("ping", {})
    r.check("ping.bridge_alive", pg.get("bridge_alive") is True,
            pg.get("ue_version"))

    mres, _dt = _t(lambda: r.call("get_asset_metrics", {
        "asset_paths": [F + "/T_CalibCube", F + "/T_BigNoise", F + "/T_FakeBig"],
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
