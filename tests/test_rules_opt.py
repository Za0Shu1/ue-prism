"""v1.5 优化族规则离线用例：5 条新规则的判定阈值、证据字段与 ROI 口径。

规则跑在 server 侧（Python>=3.10，无引擎），ctx 直接预置缓存键
（pool_entries / composition），等价于桥返回的度量已就位——真机取数路径由 L2 夹具守。
"""
from __future__ import annotations

import copy
import json
import os
import time

import pytest

from prism import rules


def _pool_project(tmp_path, value=None):
    proj = tmp_path / "Proj"
    (proj / "Content").mkdir(parents=True, exist_ok=True)
    cfg = proj / "Config"
    cfg.mkdir(parents=True, exist_ok=True)
    text = "[/Script/Engine.GameApplication]\n"
    if value is not None:
        text = "[ConsoleVariables]\nr.Streaming.PoolSize=%s\n" % value
    (cfg / "DefaultEngine.ini").write_text(text, encoding="utf-8")
    return str(proj)


_UNSET = object()


def _ctx(profile=None, entries=_UNSET, composition=_UNSET, project_dir=None):
    """预置 ctx 缓存键 = 伪装"桥已取回度量"，只测规则判定本身（取数路径归 L2 夹具）。"""
    ctx = {"profile": profile or copy.deepcopy(rules.PROFILES["pc"]),
           "scope": "/Game", "bus_dir": "", "project_dir": project_dir or ""}
    if entries is not _UNSET:
        ctx["pool_entries"] = entries
    if composition is not _UNSET:
        ctx["composition"] = composition
    return ctx


def _entry(pkg, **kw):
    base = {"package": pkg, "class": "Texture2D", "runtime_mb": 0.0, "pool_mb": 0.0,
            "permanent_mb": 0.0, "virtual_mb": 0.0, "uncompressed": False,
            "never_stream": False, "vt_supported": True, "vt_reason": None,
            "source_width": None, "effective_width": None, "max_size": None,
            "compression": None, "virtual_texture": False, "clamp_to": None,
            "clamp_savings_mb": 0.0, "vt_savings_mb": 0.0,
            "compression_savings_mb": 0.0, "actions": [], "flags": [],
            "format_class": "compressed", "non_streaming_format": False,
            "streaming_release_mb": 0.0}
    base.update(kw)
    return base


# ---------- texture_streaming_overflow ----------

def test_overflow_error_with_pool_from_ini(tmp_path):
    entries = [_entry("/Game/T/A", runtime_mb=1500.0, pool_mb=1500.0,
                      actions=["enable_vt"], vt_savings_mb=1500.0),
               _entry("/Game/T/B", runtime_mb=700.0, pool_mb=700.0,
                      actions=["clamp_max_size"], clamp_to=2048, clamp_savings_mb=500.0)]
    out = rules._rule_texture_streaming_overflow(
        _ctx(entries=entries, project_dir=_pool_project(tmp_path, "1024")))
    assert len(out) == 1
    f = out[0]
    assert f["severity"] == "error" and f["rule_id"] == "texture_streaming_overflow"
    ev = f["evidence"]
    assert ev["verdict"] == "overflow" and ev["pool_mb"] == 1024.0
    assert ev["pool_demand_mb"] == 2200.0 and ev["overflow_mb"] == 1176.0
    assert ev["pool_source"]["key"] == "r.Streaming.PoolSize"
    assert [c["package"] for c in ev["top_contributors"]][0] == "/Game/T/A"
    assert "opt_texture_clamp_max_size" in f["advice"]


def test_tight_is_warn_and_unknown_pool_does_not_claim_overflow(tmp_path):
    entries = [_entry("/Game/T/A", runtime_mb=900.0, pool_mb=900.0)]
    warn = rules._rule_texture_streaming_overflow(
        _ctx(entries=entries, project_dir=_pool_project(tmp_path, "1024")))
    assert warn[0]["severity"] == "warn" and warn[0]["evidence"]["verdict"] == "tight"
    unknown = rules._rule_texture_streaming_overflow(
        _ctx(entries=entries, project_dir=_pool_project(tmp_path, None)))
    assert unknown[0]["severity"] == "warn"
    assert unknown[0]["evidence"]["verdict"] == "unknown"
    assert "未配" in unknown[0]["advice"] or "PoolSize" in unknown[0]["advice"]


def test_small_demand_and_empty_entries_yield_nothing(tmp_path):
    entries = [_entry("/Game/T/A", runtime_mb=4.0, pool_mb=4.0)]
    assert rules._rule_texture_streaming_overflow(
        _ctx(entries=entries, project_dir=_pool_project(tmp_path, "1024"))) == []
    assert rules._rule_texture_streaming_overflow(_ctx(entries=[])) == []


def test_overflow_target_mobile_uses_lower_lines(tmp_path):
    prof = copy.deepcopy(rules.PROFILES["mobile"])
    entries = [_entry("/Game/T/A", runtime_mb=18.0, pool_mb=18.0)]
    out = rules._rule_texture_streaming_overflow(
        _ctx(profile=prof, entries=entries, project_dir=_pool_project(tmp_path, "20")))
    assert out and out[0]["severity"] == "warn"      # 90% > mobile tight_pct=85
    assert out[0]["evidence"]["verdict"] == "tight"
    # 同一份需求在 pc 线下不报（min_demand_mb=32）：阈值按平台分档是有意的
    assert rules._rule_texture_streaming_overflow(
        _ctx(entries=entries, project_dir=_pool_project(tmp_path, "20"))) == []


# ---------- oversized_mip / uncompressed_texture ----------

def _clamp_entry(pkg, px, saving, runtime=40.0):
    return _entry(pkg, source_width=px, runtime_mb=runtime, pool_mb=runtime,
                  clamp_to=2048, clamp_savings_mb=saving, actions=["clamp_max_size"])


def test_oversized_mip_severity_split():
    entries = [_clamp_entry("/Game/T/Huge", 8192, 300.0, runtime=300.0),   # error
               _clamp_entry("/Game/T/Mid", 4096, 5.0),                     # warn
               _clamp_entry("/Game/T/Tiny", 4096, 0.5)]                    # 低于收益线 -> 不报
    out = rules._rule_oversized_mip(_ctx(entries=entries))
    got = {f["subject"]: f["severity"] for f in out}
    assert got == {"/Game/T/Huge": "error", "/Game/T/Mid": "warn"}
    f = [x for x in out if x["subject"] == "/Game/T/Huge"][0]
    assert f["evidence"]["clamp_to"] == 2048 and f["threshold"] == 4096
    assert "opt_texture_clamp_max_size" in f["advice"]


def _uncompressed(pkg, **kw):
    base = dict(package=pkg, runtime_mb=40.0, pool_mb=40.0, uncompressed=True,
                format_class="uncompressed", compression="TextureCompressionSettings.TC_NONE",
                compression_savings_mb=30.0, actions=["set_compression"])
    base.update(kw)
    return _entry(pkg, **base)


def _no_stream(pkg, **kw):
    base = dict(package=pkg, runtime_mb=40.0, permanent_mb=40.0, pool_mb=0.0,
                format_class="no_stream", non_streaming_format=True,
                compression="TextureCompressionSettings.TC_EDITORICON",
                streaming_release_mb=40.0, compression_savings_mb=0.0,
                actions=["set_compression"])
    base.update(kw)
    return _entry(pkg, **base)


def test_uncompressed_texture_severity_split():
    entries = [
        _uncompressed("/Game/T/Raw"),
        _uncompressed("/Game/T/Small", runtime_mb=2.0, pool_mb=2.0,
                      compression_savings_mb=1.5),
        _uncompressed("/Game/T/Dust", runtime_mb=0.2, pool_mb=0.2,
                      compression_savings_mb=0.15),            # 低于 min_mb 不报
    ]
    out = rules._rule_uncompressed_texture(_ctx(entries=entries))
    got = {f["subject"]: f["severity"] for f in out}
    assert got == {"/Game/T/Raw": "error", "/Game/T/Small": "warn"}
    assert out[0]["evidence"]["savings_mb"] == 30.0
    assert out[0]["evidence"]["format_class"] == "uncompressed"
    assert "4B/px" in out[0]["advice"]


def test_no_stream_format_is_reported_with_permanent_ledger():
    """EditorIcon 系不进池但恒常驻：报，且收益口径是"释放常驻"而不是省池。"""
    entries = [_no_stream("/Game/UI/T_Icon"),
               _no_stream("/Game/UI/T_Dust", runtime_mb=0.2, permanent_mb=0.2,
                          streaming_release_mb=0.2),
               _entry("/Game/T/Ok", runtime_mb=40.0, pool_mb=40.0,
                      compression="TextureCompressionSettings.TC_DEFAULT")]
    out = rules._rule_uncompressed_texture(_ctx(entries=entries))
    assert [f["subject"] for f in out] == ["/Game/UI/T_Icon"]
    f = out[0]
    assert f["severity"] == "error" and f["evidence"]["format_class"] == "no_stream"
    assert f["evidence"]["permanent_mb"] == 40.0
    assert f["evidence"]["pool_mb"] == 0.0        # 别把池外常驻说成池压力
    assert f["evidence"]["savings_mb"] == 40.0
    assert "恒常驻" in f["advice"] and "opt_texture_set_compression" in f["advice"]
    assert "4B/px" not in f["advice"]


def test_unknown_compression_format_is_not_a_finding():
    """判不准的格式文本一律不报：宁可漏报也不误判美术贴图。"""
    out = rules._rule_uncompressed_texture(
        _ctx(entries=[_entry("/Game/T/Weird", runtime_mb=40.0, pool_mb=40.0,
                             format_class="unknown", uncompressed=False,
                             compression=u"编辑器图标")]))
    assert out == []


def _high_bpp(pkg, **kw):
    base = {"class": "TextureCube", "runtime_mb": 192.0, "pool_mb": 192.0,
            "high_bpp": True, "format_class": "high_bpp",
            "compression": "TextureCompressionSettings.TC_HDR",
            "compression_savings_mb": 168.0, "runtime_bytes_per_texel": 8.0,
            "runtime_basis": "resident_memory"}
    base.update(kw)
    return _entry(pkg, **base)


def test_high_bpp_texture_is_a_finding_but_not_an_auto_action():
    """真机回归：TC_HDR 曾被归"压缩格式"直接漏报，192MB 立方图静默隐身。
    现在按实测 8B/px 报账，但建议必须写明"丢 HDR 动态范围 / 默认工具不动"。"""
    entries = [_high_bpp("/Game/ClientLib/Material/Textures/T_HDR"),
               _high_bpp("/Game/T/SmallHdr", runtime_mb=2.0, pool_mb=2.0,
                         compression_savings_mb=1.75),
               _high_bpp("/Game/T/HdrDust", runtime_mb=0.2, pool_mb=0.2,
                         compression_savings_mb=0.175)]
    out = rules._rule_uncompressed_texture(_ctx(entries=entries))
    got = {f["subject"]: f["severity"] for f in out}
    assert got == {"/Game/ClientLib/Material/Textures/T_HDR": "error",
                   "/Game/T/SmallHdr": "warn"}
    f = out[0]
    assert f["evidence"]["format_class"] == "high_bpp"
    assert f["evidence"]["runtime_bytes_per_texel"] == 8.0
    assert f["evidence"]["runtime_basis"] == "resident_memory"
    assert f["evidence"]["savings_mb"] == 168.0
    assert f["evidence"]["pool_mb"] == 192.0
    assert "8B/px" in f["advice"] and "only_uncompressed=False" in f["advice"]
    assert "4B/px" not in f["advice"]          # 别把高位深混成未压缩系的账


# ---------- nanite_translucent_conflict（离线直读编辑器日志）----------

LOG_OK = (u"[2026.10.08-10.00.00:000][  12]LogStaticMesh: Warning: The material "
          u"/Game/Mats/M_Glass.M_Glass has a blending mode of 'Translucent' which isn't "
          u"supported by Nanite mesh /Game/Meshes/SM_Window.SM_Window\n")
LOG_OTHER = (u"[2026.10.08-10.00.01:000][  13]LogMaterial: Warning: Missing unused thing "
             u"/Game/Mats/M_Other.M_Other\n")


def _log(tmp_path, text, name="UEPrism.log"):
    d = tmp_path / "Proj" / "Saved" / "Logs"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(text, encoding="utf-8")
    return str(tmp_path / "Proj")


def test_nanite_translucent_attributed_to_assets(tmp_path):
    proj = _log(tmp_path, LOG_OK * 2 + LOG_OTHER)
    out = rules._rule_nanite_translucent_conflict(_ctx(project_dir=proj))
    assert len(out) == 1
    f = out[0]
    assert f["severity"] == "error" and f["rule_id"] == "nanite_translucent_conflict"
    assert f["evidence"]["warning_lines"] == 2
    assert f["evidence"]["distinct_assets"] == 2
    got = {a["package"] for a in f["evidence"]["assets"]}
    assert got == {"/Game/Mats/M_Glass", "/Game/Meshes/SM_Window"}
    assert "fix_nanite_translucent" in f["advice"]


def test_nanite_translucent_silent_when_no_match(tmp_path):
    proj = _log(tmp_path, LOG_OTHER)
    assert rules._rule_nanite_translucent_conflict(_ctx(project_dir=proj)) == []


def test_nanite_translucent_missing_log_raises_not_silent(tmp_path):
    proj = tmp_path / "NoLogs"
    proj.mkdir(parents=True)
    with pytest.raises(FileNotFoundError):
        rules._rule_nanite_translucent_conflict(_ctx(project_dir=str(proj)))


# ---------- drawcall_batching ----------

def _comp(opps, share, total=2000):
    return {"instancing_opportunities": opps, "instanced_share_pct": share,
            "plain_staticmesh_actors": sum(o["actors"] for o in opps),
            "world_total_actors": total}


def test_batching_error_when_nothing_instanced():
    comp = _comp([{"mesh": "/Game/M/SM_A", "actors": 171},
                  {"mesh": "/Game/M/SM_B", "actors": 60}], share=0.0)
    out = rules._rule_drawcall_batching(_ctx(composition=comp))
    assert out and out[0]["severity"] == "error"
    ev = out[0]["evidence"]
    assert ev["collapsible_actors"] == 229 and ev["groups"] == 2
    assert ev["top_groups"][0]["mesh"] == "/Game/M/SM_A"
    assert "analyze_instancing" in out[0]["advice"]


def test_batching_warn_when_partially_instanced_and_floor_respected():
    comp = _comp([{"mesh": "/Game/M/SM_A", "actors": 171}], share=40.0)
    out = rules._rule_drawcall_batching(_ctx(composition=comp))
    assert out and out[0]["severity"] == "warn"       # 已有部分实例化 -> 降级为 warn
    assert out[0]["evidence"]["collapsible_actors"] == 170
    tiny = _comp([{"mesh": "/Game/M/SM_A", "actors": 12}], share=40.0)
    assert rules._rule_drawcall_batching(_ctx(composition=tiny)) == []   # <min_collapsible
    small_group = _comp([{"mesh": "/Game/M/SM_A", "actors": 4}], share=0.0)
    assert rules._rule_drawcall_batching(_ctx(composition=small_group)) == []  # <min_group


def test_batching_no_composition_is_not_a_finding():
    assert rules._rule_drawcall_batching(_ctx(composition=None)) == []   # 桥没给构成
    assert rules._rule_drawcall_batching(_ctx(composition={"instancing_opportunities": [],
                                                            "instanced_share_pct": 0.0})) == []


# ---------- ROI 口径（别把重叠收益加进总 MB）----------

def test_roi_units_and_efforts_for_new_rules(tmp_path):
    entries = [_clamp_entry("/Game/T/Huge", 8192, 300.0, runtime=300.0),
               _uncompressed("/Game/T/Raw"),
               _high_bpp("/Game/T/HdrCube"),
               _no_stream("/Game/UI/T_Icon", runtime_mb=20.0, permanent_mb=20.0,
                          streaming_release_mb=20.0)]
    findings = []
    findings += rules._rule_oversized_mip(_ctx(entries=entries))
    findings += rules._rule_uncompressed_texture(_ctx(entries=entries))
    findings += rules._rule_texture_streaming_overflow(
        _ctx(entries=entries, project_dir=_pool_project(tmp_path, "128")))
    findings += rules._rule_drawcall_batching(
        _ctx(composition=_comp([{"mesh": "/Game/M/A", "actors": 200}], share=0.0)))
    findings += rules._rule_nanite_translucent_conflict(_ctx(project_dir=_log(tmp_path, LOG_OK)))
    summary = rules.annotate_roi(findings)
    by_rule = {f["rule_id"]: f["roi"] for f in findings}
    assert by_rule["oversized_mip"]["unit"] == "memory_or_disk_mb"
    assert by_rule["oversized_mip"]["effort"] == "low"
    uncomp = [f for f in findings if f["rule_id"] == "uncompressed_texture"]
    units = {f["subject"]: f["roi"]["unit"] for f in uncomp}
    assert units == {"/Game/T/Raw": "pool_uncompressed_mb",
                     "/Game/UI/T_Icon": "permanent_release_mb",
                     "/Game/T/HdrCube": "pool_high_bpp_mb"}
    hdr = [f for f in findings if f["subject"] == "/Game/T/HdrCube"][0]
    assert hdr["roi"]["benefit"] == 168.0       # 高位深收益单列，不并入 MB 总计
    assert by_rule["uncompressed_texture"]["effort"] == "low"
    assert by_rule["texture_streaming_overflow"]["unit"] == "pool_overflow_mb"
    assert by_rule["drawcall_batching"]["unit"] == "collapsible_actors"
    assert by_rule["drawcall_batching"]["effort"] == "high"
    assert by_rule["nanite_translucent_conflict"]["unit"] == "warning_lines"
    # 只有不重叠的 oversized_mip 计入 MB 总计（300）；改压缩的 30/20 是重叠收益，不叠加
    assert summary["total_benefit_mb"] == 300.0
    # low 四条：钳尺寸 + 未压缩 + 高位深 + 禁流送；high 一条：折叠 HISM
    assert summary["by_effort"]["low"] == 4 and summary["by_effort"]["high"] == 1


# ---------- 报告纪律：离线半份报告不得因新规则"看起来更干净" ----------

def test_offline_report_skips_new_bridge_rules_loudly(tmp_path):
    proj = _pool_project(tmp_path, "1024")
    b = tmp_path / "Prism"
    b.mkdir()
    (b / "heartbeat.json").write_text(json.dumps({"ts": time.time() - 999.0}))
    rep = rules.run_report(proj, str(b))
    assert rep["bridge"] == "offline"
    skips = " ".join(rep["summary"]["skipped_rules"])
    for rid in ("texture_streaming_overflow", "oversized_mip", "uncompressed_texture",
                "drawcall_batching"):
        assert rid + ": bridge offline" in skips
    assert rid_not_in_findings(rep["findings"])
    assert rep["total"] == 0 and rep["truncated"] is False and rep["cap"] == rules.DEFAULT_CAP
    assert os.path.isdir(os.path.join(proj, "Config"))


def rid_not_in_findings(findings):
    new_ids = {"texture_streaming_overflow", "oversized_mip", "uncompressed_texture",
               "drawcall_batching", "nanite_translucent_conflict"}
    return not any(f["rule_id"] in new_ids for f in findings)


def test_list_rules_exposes_new_thresholds():
    out = rules.list_rules(target="mobile", project_dir="")
    ids = {r["id"] for r in out["rules"]}
    assert {"oversized_mip", "uncompressed_texture", "texture_streaming_overflow",
            "nanite_translucent_conflict", "drawcall_batching"} <= ids
    mip = [r for r in out["rules"] if r["id"] == "oversized_mip"][0]
    assert mip["thresholds"]["min_savings_mb"] == 2.0          # mobile 更激进
    assert mip["channel"] == "bridge"
    batch = [r for r in out["rules"] if r["id"] == "drawcall_batching"][0]
    assert batch["thresholds"]["min_group_actors"] == 4



# ---------- nanite_translucent_conflict：真机误报回归 ----------

# 5.4 真机实跑 get_perf_report 暴露：LogMaterial 的着色器统计行是 Display 级、单条上百 KB，
# 同时含 "nanite" 与 "translucent"，但不是冲突警告，不该炸出一条 error。
LOG_SHADER_STATS = (u"[2026.10.10-09.00.00:000][   1]LogMaterial: Display: "
                    u"WorldGridMaterial-SM6-Default: MATSM_CalibNanite-Tess0-PDOFF-"
                    u"SHADERMODEL6-TRANSLUCENT-LIGHTMAPQUALITY0--SHADER_TYPE_"
                    u"MATERIALLICENSE: StaticPerCluster=1\n")

# 5.3 真实工程（ProjectA，4623 条同源）实测原文：名字在方括号里、不带 /Game 路径
LOG_53_REAL = (u"[2026.09.29-08.00.00:000][   7]LogStaticMesh: Warning: Invalid material "
               u"[M_GXNQ_building_LJH_21_06_01] used on Nanite static mesh [SM_Tower007]. "
               u"Only opaque or masked blend modes are currently supported, "
               u"[BLEND_Translucent] blend mode is not supported with Nanite enabled.\n")


def test_nanite_translucent_display_stats_line_is_not_a_finding(tmp_path):
    proj = _log(tmp_path, LOG_SHADER_STATS * 20 + LOG_OTHER)
    assert rules._rule_nanite_translucent_conflict(_ctx(project_dir=proj)) == []


def test_nanite_translucent_non_warning_level_is_ignored(tmp_path):
    proj = _log(tmp_path, LOG_53_REAL.replace("Warning:", "Display:"))
    assert rules._rule_nanite_translucent_conflict(_ctx(project_dir=proj)) == []


def test_nanite_translucent_real_53_line_attributes_by_name(tmp_path):
    proj = _log(tmp_path, LOG_53_REAL * 3)
    out = rules._rule_nanite_translucent_conflict(_ctx(project_dir=proj))
    assert len(out) == 1
    ev = out[0]["evidence"]
    assert ev["warning_lines"] == 3 and ev["attribution"] == "asset_names_only"
    got = {n["name"] for n in ev["names"]}
    # BLEND_Translucent 是同行尾的枚举 token，不是资产名（5.4 夹具日志实证），不入名单
    assert got == {u"M_GXNQ_building_LJH_21_06_01", u"SM_Tower007"}
    assert u"/Game" in out[0]["subject"]        # 如实说明该行不带包路径，按名归因
    assert u"帧" not in out[0]["subject"]        # 时间戳/帧计数没被当成资产名


def test_nanite_translucent_package_paths_still_win(tmp_path):
    proj = _log(tmp_path, LOG_53_REAL + LOG_OK)
    ev = rules._rule_nanite_translucent_conflict(_ctx(project_dir=proj))[0]["evidence"]
    assert ev["attribution"] == "package_paths" and ev["distinct_assets"] == 2


# ---------- 安全阀（#11）：撤金额不撤发现 ----------


def _cold_clamp_entry(pkg, **kw):
    """真实工程 hDRI 型读数：边长 2365 是反推估计，收益被安全阀撤回（None）。"""
    base = {"source_width": 2365, "size_basis": "source_unverified",
            "flags": ["size_unverified", "oversized_mip_chain"],
            "runtime_mb": 340.5, "pool_mb": 340.5, "clamp_to": 2048,
            "clamp_savings_mb": None, "actions": ["clamp_max_size"]}
    base.update(kw)
    return _entry(pkg, **base)


def test_oversized_mip_survives_withheld_savings_as_warn():
    out = rules._rule_oversized_mip(_ctx(entries=[_cold_clamp_entry("/Game/T/ColdCube")]))
    assert len(out) == 1                      # 不报价绝不能让 finding 隐身（#7 教训）
    f = out[0]
    assert f["severity"] == "warn"            # 没把握的收益不配 error 档
    assert "savings_mb" not in f["evidence"]  # None 不写进证据，也不谎报 0.0
    assert f["evidence"]["size_unverified"] is True
    assert f["evidence"]["size_basis"] == "source_unverified"
    assert f["evidence"]["clamp_to"] == 2048
    assert "opt_texture_clamp_max_size" in f["advice"] and "复测" in f["advice"]
    summary = rules.annotate_roi([f])
    assert summary["total_benefit_mb"] == 0.0 and summary["actionable_size"] == 0


def test_oversized_mip_mixed_window_keeps_verified_error_and_savings():
    entries = [_clamp_entry("/Game/T/Huge", 8192, 300.0, runtime=300.0),
               _cold_clamp_entry("/Game/T/ColdCube")]
    got = {f["subject"]: f["severity"] for f in rules._rule_oversized_mip(_ctx(entries=entries))}
    assert got == {"/Game/T/Huge": "error", "/Game/T/ColdCube": "warn"}
    warm = [f for f in rules._rule_oversized_mip(_ctx(entries=entries))
            if f["subject"] == "/Game/T/Huge"][0]
    assert warm["evidence"]["savings_mb"] == 300.0


def test_uncompressed_texture_keeps_finding_without_money():
    """格式是直读属性、不依赖边长：安全阀只撤金额，档位/建议/动作照给。"""
    entries = [_uncompressed("/Game/T/RawCold", compression_savings_mb=None,
                             source_width=2365, size_basis="source_unverified",
                             flags=["size_unverified"]),
               _no_stream("/Game/UI/T_IconCold", streaming_release_mb=None,
                          size_basis="source_unverified", flags=["size_unverified"]),
               _high_bpp("/Game/T/ColdHdr", compression_savings_mb=None,
                         size_basis="source_unverified", flags=["size_unverified"])]
    out = rules._rule_uncompressed_texture(_ctx(entries=entries))
    assert len(out) == 3
    by = {f["subject"]: f for f in out}
    for f in out:
        assert "savings_mb" not in f["evidence"]
        assert f["evidence"]["size_unverified"] is True
    raw = by["/Game/T/RawCold"]
    assert raw["severity"] == "error"          # 档位看台账 MB，不是看收益
    assert "4B/px" in raw["advice"] and "金额暂不报价" in raw["advice"]
    icon = by["/Game/UI/T_IconCold"]
    assert "恒常驻" in icon["advice"] and icon["evidence"]["permanent_mb"] == 40.0
    assert "only_uncompressed=False" in by["/Game/T/ColdHdr"]["advice"]


def test_overflow_discloses_unverified_share_of_pool_demand(tmp_path):
    entries = [_cold_clamp_entry("/Game/T/ColdCube"),
               _entry("/Game/T/Warm", runtime_mb=200.0, pool_mb=200.0)]
    out = rules._rule_texture_streaming_overflow(
        _ctx(entries=entries, project_dir=_pool_project(tmp_path, "300")))
    ev = out[0]["evidence"]
    assert ev["size_unverified_count"] == 1
    assert ev["size_unverified_pool_mb"] == 340.5
    assert ev["savings_withheld_count"] == 1
    assert "反推边长" in out[0]["advice"]
