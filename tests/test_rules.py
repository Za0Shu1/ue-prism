"""PR-A 规则引擎测试：profile 加载/TOML 覆盖/两条离线规则/报告纪律（无引擎）。"""
from __future__ import annotations

import os

import pytest

from prism import envelope, rules, server, tasks

TOML = (
    '[profile]\ntarget = "pc"\n\n'
    "[rules.asset_size_mb]\nwarn = 0.001\nerror = 0.002\n"
)


def _proj(tmp_path, toml=None, content=True):
    proj = tmp_path / "Proj"
    if content:
        (proj / "Content" / "Bad").mkdir(parents=True)
        (proj / "Content" / "Bad" / "Big.uasset").write_bytes(b"y" * 3000)  # 0.0029MB
        (proj / "Content" / "ok.uasset").write_bytes(b"z" * 10)
    else:
        proj.mkdir(parents=True)
    if toml:
        (proj / "prism.toml").write_text(toml, encoding="utf-8")
    return str(proj)


def _bus(tmp_path):
    b = tmp_path / "Prism"
    b.mkdir()
    import json as _json, time as _t
    (b / "heartbeat.json").write_text(_json.dumps({"ts": _t.time() - 999.0}))  # 陈旧心跳->在线规则即刻skip
    return str(b)


def _drop_task(b, proj, kind="cook", status="succeeded", pkg="/Game/Gone/Mesh"):
    log = os.path.join(b, "%s_drop_%d.log" % (kind, len(os.listdir(b))))
    with open(log, "w", encoding="utf-8") as f:
        f.write("[t][ 0]LogCook: Display: cooking\n")
        f.write("[t][ 1]LogAssetRegistry: Warning: ScanPathsSynchronous: Package %s does not exist, will not scan.\n" % pkg)
        f.write("BUILD SUCCESSFUL\n")
    rec = tasks.create(b, kind, proj, "cmd", log)
    if status == "succeeded":
        tasks.mark_done(b, rec["task_id"], 0)
    else:
        tasks.mark_done(b, rec["task_id"], 1)
    return rec


def test_load_profile_defaults(tmp_path):
    prof, tgt = rules.load_profile(_proj(tmp_path))
    assert tgt == "pc" and prof["asset_size_mb"]["warn"] == 20.0


def test_load_profile_unknown_target(tmp_path):
    with pytest.raises(ValueError):
        rules.load_profile(_proj(tmp_path), target="atari")


def test_toml_override_and_report(tmp_path):
    proj = _proj(tmp_path, toml=TOML)
    b = _bus(tmp_path)
    rep = rules.run_report(proj, b)
    assert rep["profile"] == "pc" and rep["engine"] is None
    assert rep["summary"]["error"] == 1 and rep["summary"]["warn"] == 0
    f = rep["findings"][0]
    assert f["rule_id"] == "asset_size_top" and f["subject"] == "/Game/Bad/Big"
    assert f["threshold"] == 0.002 and f["severity"] == "error"
    assert rep["total"] == 1 and rep["truncated"] is False and rep["cap"] == rules.DEFAULT_CAP


def test_cook_drop_only_for_succeeded(tmp_path):
    proj = _proj(tmp_path, toml=TOML)
    b = _bus(tmp_path)
    _drop_task(b, proj, kind="cook", status="succeeded")
    _drop_task(b, proj, kind="cook", status="failed", pkg="/Game/Other/Ghost")
    rep = rules.run_report(proj, b)
    drops = [f for f in rep["findings"] if f["rule_id"] == "cook_drop"]
    assert len(drops) == 1
    assert drops[0]["subject"] == "/Game/Gone/Mesh"
    assert drops[0]["severity"] == "error"
    assert "task_id" in drops[0]["evidence"]


def test_missing_content_or_bad_scope_errors(tmp_path):
    """契约更新：Content 缺失 / scope 目录不存在 / 窗口参数非法 -> 显式 ValueError（server 层转
    RUNTIME_ERROR），不再静默跳过规则产出伪装成功的残缺报告。"""
    b = _bus(tmp_path)
    with pytest.raises(ValueError):
        rules.run_report(_proj(tmp_path, content=False), b)
    # 换一个独立工程目录再测 scope 校验（_proj 固定路径，不可同 tmp_path 连建两次）
    proj = str(tmp_path / "Proj2")
    os.makedirs(os.path.join(proj, "Content", "Bad"))
    open(os.path.join(proj, "Content", "Bad", "Big.uasset"), "wb").write(b"y" * 3000)
    open(os.path.join(proj, "prism.toml"), "w").write(TOML)
    with pytest.raises(ValueError):
        rules.run_report(proj, b, scope="project")                # 当年把 scope 当字面子目录的误用
    rep = rules.run_report(proj, b, scope="Bad")                  # 现有文件夹（可省 /Game 前缀）照常出报告
    assert rep["summary"]["error"] == 1 and rep["findings"][0]["subject"] == "/Game/Bad/Big"
    with pytest.raises(ValueError):
        rules.run_report(proj, b, recent_tasks="many")            # 窗口参数也要报错而非吞掉


def test_server_tool_wiring(tmp_path):
    proj = _proj(tmp_path, toml=TOML)
    b = _bus(tmp_path)
    _drop_task(b, proj)
    server._CFG["project_dir"] = proj
    server._CFG["bus_dir"] = b
    try:
        env = server.get_perf_report()
        assert env["ok"] and env["result"]["summary"]["error"] == 2  # 1 size + 1 drop
        bad = server.get_perf_report(target="atari")
        assert bad["ok"] is False and bad["error"]["code"] == envelope.Code.RUNTIME_ERROR
        lst = server.list_perf_rules()
        assert lst["ok"] and lst["result"]["total"] == 7
        ids = {r["id"] for r in lst["result"]["rules"]}
        assert ids == {"asset_size_top", "cook_drop", "wps_external_actors",
                       "texture_size", "mesh_tri", "scene_light_dup", "cook_empty_maps"}
        size_rule = [r for r in lst["result"]["rules"] if r["id"] == "asset_size_top"][0]
        assert size_rule["thresholds"]["warn"] == 0.001  # 工程 TOML 生效
    finally:
        server._CFG["project_dir"] = None
        server._CFG["bus_dir"] = None


# ---------- PR-C：桥通道规则 ----------

def _stale_heartbeat(b):
    import json, time
    with open(os.path.join(b, "heartbeat.json"), "w") as f:
        json.dump({"ts": time.time() - 999.0}, f)


def _live_bridge(b):
    import threading, time as _t
    from prism import bridge
    threading.Thread(target=bridge.run_forever, args=(b, 0.02), daemon=True).start()
    import json
    with open(os.path.join(b, "heartbeat.json"), "w") as f:
        json.dump({"ts": _t.time()}, f)


def test_bridge_offline_skips_bridge_rules(tmp_path):
    proj = _proj(tmp_path, toml=TOML)
    b = _bus(tmp_path)
    _stale_heartbeat(b)
    rep = rules.run_report(proj, b)
    assert rep["bridge"] == "offline"
    skips = [x for x in rep["summary"]["skipped_rules"] if x.endswith(": bridge offline")]
    assert any(x.startswith("texture_size") for x in skips)
    assert any(x.startswith("mesh_tri") for x in skips)
    assert any(x.startswith("scene_light_dup") for x in skips)
    # 离线规则照常工作
    assert any(f["rule_id"] == "asset_size_top" for f in rep["findings"])


def test_bridge_online_degraded_bridge(tmp_path):
    proj = _proj(tmp_path, toml=TOML)
    b = _bus(tmp_path)
    _live_bridge(b)
    rep = rules.run_report(proj, b)
    assert rep["bridge"] == "online"
    assert not any("bridge offline" in x for x in rep["summary"]["skipped_rules"])
    assert not any(f["rule_id"] in ("texture_size", "mesh_tri", "scene_light_dup") for f in rep["findings"])


def test_wps_external_actors_rule(tmp_path):
    toml = '[rules.wps_external_actors]\nwarn = 2\nerror = 5\n'
    proj = os.path.join(str(tmp_path), "P2")
    ea = os.path.join(proj, "Content", "__ExternalActors__", "M")
    os.makedirs(ea)
    for i in range(3):
        open(os.path.join(ea, "A%d.uasset" % i), "wb").close()
    open(os.path.join(proj, "prism.toml"), "w").write(toml)
    b = str(tmp_path / "Prism2")
    os.makedirs(b)
    _stale_heartbeat(b)
    rep = rules.run_report(proj, b)
    hits = [f for f in rep["findings"] if f["rule_id"] == "wps_external_actors"]
    assert len(hits) == 1 and hits[0]["severity"] == "warn" and hits[0]["evidence"]["count"] == 3


def test_texture_rule_unit():
    ctx = {"profile": {"texture_px": {"warn": 2048, "error": 4096}},
           "metrics": {"/Game/T": {"metrics": {"width": 4096, "height": 4096, "memory_bytes": 67108864}}}}
    out = rules._rule_texture_size(ctx)
    assert len(out) == 1 and out[0]["severity"] == "error" and out[0]["threshold"] == 4096


def test_mesh_rule_unit_single_lod():
    ctx = {"profile": {"mesh_tri": {"warn": 200000, "error": 1000000}},
           "metrics": {"/Game/M": {"metrics": {"lods": [{"index": 0, "triangles": 250000}], "lod_count": 1}}}}
    out = rules._rule_mesh_tri(ctx)
    assert len(out) == 1 and out[0]["severity"] == "warn"
    assert "single-LOD" in out[0]["advice"]


def test_scene_light_dup_unit():
    ctx = {"profile": {"scene_light_dup": {"max_dup": 1}}, "actors": [
        {"class": "DirectionalLight", "name": "a"}, {"class": "DirectionalLight", "name": "b"},
        {"class": "SkyLight", "name": "c"}, {"class": "PlayerStart", "name": "d"}]}
    out = rules._rule_scene_light_dup(ctx)
    assert len(out) == 1 and out[0]["evidence"]["counts"] == {"DirectionalLight": 2}
    assert out[0]["subject"] == "DirectionalLight x2"   # subject 是灯光计数而非 scope 串


# ---------- PR-D：cook_empty_maps ----------

def _task_with_log(b, proj, command, log_body, status="succeeded"):
    log = os.path.join(b, "d_%d.log" % len(os.listdir(b)))
    open(log, "w", encoding="utf-8").write(log_body)
    rec = tasks.create(b, "cook", proj, command, log)
    tasks.mark_done(b, rec["task_id"], 0 if status == "succeeded" else 1)
    return rec


COOKED_OK = ("LogCook: Display: Splitting Package /Game/NewMap with splitter X acting on object\n"
             "LogCook: Display: Cooked packages 459 Packages Remain 0 Total 459\nBUILD SUCCESSFUL\n")

def test_empty_maps_rule_matrix(tmp_path):
    proj = _proj(tmp_path, toml=TOML)
    b = _bus(tmp_path)
    _task_with_log(b, proj, 'UAT BuildCookRun +maps="/Game/NoSuchMapXYZ"', COOKED_OK)
    _task_with_log(b, proj, 'UAT BuildCookRun +maps="/Game/NewMap"', COOKED_OK)
    _task_with_log(b, proj, "UAT BuildCookRun",
                   "LogCook: Display: Cooked packages 0 Packages Remain 0 Total 0\nBUILD SUCCESSFUL\n")
    _task_with_log(b, proj, 'UAT BuildCookRun +maps="/Game/Ghost"', "whatever", status="failed")
    rep = rules.run_report(proj, b)
    hits = [f for f in rep["findings"] if f["rule_id"] == "cook_empty_maps"]
    subs = {f["subject"]: f for f in hits}
    assert subs["/Game/NoSuchMapXYZ"]["severity"] == "error"          # 请求了但没煮
    assert "/Game/NewMap" not in subs                                  # 正常煮过 -> 不报
    assert "cook-2" in "".join(subs) or any(f["evidence"].get("check") == "zero_package_cook" for f in hits)
    assert "/Game/Ghost" not in subs                                   # 失败任务不参与

def test_sort_is_stable_and_grouped(tmp_path):
    proj = _proj(tmp_path, toml=TOML)
    b = _bus(tmp_path)
    _task_with_log(b, proj, 'UAT +maps="/Game/Zeta"', COOKED_OK)
    _task_with_log(b, proj, 'UAT +maps="/Game/Alpha"', COOKED_OK)
    rep = rules.run_report(proj, b)
    errs = [f for f in rep["findings"] if f["severity"] == "error"]
    order = [(f["rule_id"], f["subject"]) for f in errs]
    assert order == sorted(order)  # 同 severity 内 rule_id+subject 字典序 -> 报告可 diff


def test_command_echo_line_not_treated_as_cooked(tmp_path):
    """回归：UAT 回显行原样带 +maps，不得据此判"已煮"（真机踩坑）。"""
    proj = _proj(tmp_path, toml=TOML)
    b = _bus(tmp_path)
    body = ("Parsing command line: BuildCookRun +maps=\"/Game/BadName\" -unattended\n" + COOKED_OK)
    _task_with_log(b, proj, 'UAT BuildCookRun +maps="/Game/BadName"', body)
    rep = rules.run_report(proj, b)
    hits = [f for f in rep["findings"]
            if f["rule_id"] == "cook_empty_maps" and f["subject"] == "/Game/BadName"]
    assert len(hits) == 1 and hits[0]["severity"] == "error"


import sys


@pytest.mark.skipif(sys.version_info >= (3, 11),
                    reason="tomllib 原生可用；此回退分支只在 Python<3.11 生效（CI 3.10 守此）")
def test_tomllib_falls_back_to_tomli_on_py310():
    """Python 3.10 无 tomllib，rules 须回退到 tomli（否则 server 整链 import 崩）。"""
    assert rules.tomllib.__name__ == "tomli"
    # loads 收 str（tomllib/tomli 一致）；能解析即用
    data = rules.tomllib.loads("[profile]\ntarget = \"pc\"\n")
    assert data["profile"]["target"] == "pc"


# ---------- 档案窗口：陈旧 cook 证据自动过期 ----------

def test_stale_cook_findings_expire_out_of_window(tmp_path):
    """drop 只出现在最老的成功档案里：默认窗口(3)被更新的干净 cook 取代后不再报警；0=全部可取证。"""
    proj = _proj(tmp_path)
    b = _bus(tmp_path)
    old = _drop_task(b, proj)
    for _ in range(3):
        _task_with_log(b, proj, 'UAT BuildCookRun +maps="/Game/NewMap"', COOKED_OK)
    rep = rules.run_report(proj, b)
    assert [f for f in rep["findings"] if f["rule_id"] == "cook_drop"] == []
    assert rep["cook_archive"]["archive_total"] == 4
    assert rep["cook_archive"]["window_task_ids"][0] != old["task_id"]
    assert old["task_id"] not in rep["cook_archive"]["window_task_ids"]
    rep_all = rules.run_report(proj, b, recent_tasks=0)
    drops = [f for f in rep_all["findings"] if f["rule_id"] == "cook_drop"]
    assert len(drops) == 1 and drops[0]["evidence"]["task_id"] == old["task_id"]


def test_cook_drop_dedup_keeps_newest(tmp_path):
    """同一资产在窗口内多份档案重复 drop：只归因最新一份，并携带 task_created_ts。"""
    proj = _proj(tmp_path)
    b = _bus(tmp_path)
    _drop_task(b, proj)
    _task_with_log(b, proj, 'UAT BuildCookRun +maps="/Game/NewMap"', COOKED_OK)
    new = _drop_task(b, proj)
    rep = rules.run_report(proj, b)
    hits = [f for f in rep["findings"] if f["rule_id"] == "cook_drop"]
    assert len(hits) == 1
    assert hits[0]["evidence"]["task_id"] == new["task_id"]
    assert hits[0]["evidence"]["task_created_ts"] == new["created_ts"]


def test_server_perf_report_scope_and_window(tmp_path):
    """server 层契约：坏 scope -> ok:false 的 RUNTIME_ERROR；窗口元数据随报告返回。"""
    proj = _proj(tmp_path)
    b = _bus(tmp_path)
    server._CFG["project_dir"] = proj
    server._CFG["bus_dir"] = b
    try:
        bad = server.get_perf_report(scope="project")
        assert bad["ok"] is False and bad["error"]["code"] == envelope.Code.RUNTIME_ERROR
        assert "scope" in bad["error"]["message"]
        ok = server.get_perf_report(recent_tasks="1")   # MCP 字符串送达也要可用
        assert ok["ok"] and ok["result"]["cook_archive"]["recent_tasks"] == 1
    finally:
        server._CFG["project_dir"] = None
        server._CFG["bus_dir"] = None


def test_noop_incremental_cook_not_flagged(tmp_path):
    """回归(-iterate 全 up-to-date)：日志无该 map 逐包行，但磁盘 cook 产物在 -> 不得误报 map_not_cooked；
    产物缺失(真·坏 map 名)仍必须报错。"""
    proj = _proj(tmp_path)
    b = _bus(tmp_path)
    noop_log = ("LogCook: Display: Keeping 453. Recooking 0. Removing 0.\n"
                "LogCook: Display: Cooked packages 466 Packages Remain 0 Total 466\nBUILD SUCCESSFUL\n")
    _task_with_log(b, proj, 'UAT BuildCookRun -iterate +maps="/Game/NewMap"', noop_log)
    # 产物不存在 -> 必须报（保守方向：宁可多报也不漏报）
    rep = rules.run_report(proj, b)
    hits = [f for f in rep["findings"] if f["rule_id"] == "cook_empty_maps"]
    assert len(hits) == 1 and hits[0]["subject"] == "/Game/NewMap"
    assert hits[0]["evidence"]["disk_check"] == "cooked_artifact_absent"
    # 落盘 Saved/Cooked/Windows/Proj/Content/NewMap.umap -> 同一条日志不再报
    umap = os.path.join(proj, "Saved", "Cooked", "Windows", "Proj", "Content", "NewMap.umap")
    os.makedirs(os.path.dirname(umap))
    open(umap, "wb").write(b"x")
    rep2 = rules.run_report(proj, b)
    assert [f for f in rep2["findings"] if f["rule_id"] == "cook_empty_maps"] == []


def test_cook_drop_disk_recheck(tmp_path):
    """现状复核：窗口内任务的 drop 若资产现已在磁盘(修复后待重 cook) -> 静默；
    磁盘确实没有 -> 仍报 error 并带 disk_check=package_absent。"""
    proj = _proj(tmp_path)
    b = _bus(tmp_path)
    _drop_task(b, proj, pkg="/Game/StillGone/Mesh")
    rep_missing = rules.run_report(proj, b)
    hits = [f for f in rep_missing["findings"] if f["rule_id"] == "cook_drop"]
    assert len(hits) == 1
    assert hits[0]["subject"] == "/Game/StillGone/Mesh"
    assert hits[0]["evidence"]["disk_check"] == "package_absent"
    # 把其中一个被 drop 的包"修好"(落盘 .uasset) 并再造一条同包 drop 档案 -> 复核后不报
    fixed = os.path.join(proj, "Content", "Fixed")
    os.makedirs(fixed)
    open(os.path.join(fixed, "Mesh.uasset"), "wb").write(b"z")
    _drop_task(b, proj, pkg="/Game/Fixed/Mesh")
    rep_fixed = rules.run_report(proj, b)
    subs = {f["subject"] for f in rep_fixed["findings"] if f["rule_id"] == "cook_drop"}
    assert "/Game/Fixed/Mesh" not in subs and "/Game/StillGone/Mesh" in subs
