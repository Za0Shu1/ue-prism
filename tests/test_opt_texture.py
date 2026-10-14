"""v1.5 纹理工具族离线用例：无引擎降级结构、双钥守卫、建议文案、参数校验。

真机属性读写路径进 L2 夹具（docs/TESTING.md），这里守的是"没引擎时也必须诚实"。
"""
from __future__ import annotations

from prism import envelope
from prism.domain import opt_texture as ot

SCAN_KEYS = {"items", "summary", "candidates", "pool", "sampling", "total",
             "truncated", "cap", "note", "model", "tried", "scope"}
WRITE_KEYS = {"op", "dry_run", "executed", "success", "targets", "changed", "no_op",
              "skipped", "failed", "planned", "est_savings_mb",
              "est_permanent_release_mb", "universe", "total",
              "truncated", "cap", "note", "model", "tried", "scope", "rejected"}


def test_scan_degrades_with_structure_keys():
    r = ot.scan_texture_streaming(folder="/Game/Textures", top=5)
    assert r["note"] == "no_engine"
    assert SCAN_KEYS <= set(r)
    assert r["items"] == [] and r["total"] == 0 and r["cap"] == 5
    assert r["summary"]["pool_demand_mb"] == 0.0
    assert r["model"]["version"]                      # 口径常驻，消费方不必猜
    assert set(r["candidates"]) == {"need_clamp", "need_vt", "uncompressed",
                                    "high_bpp", "non_streaming", "vt_blocked",
                                    "never_stream"}


def test_scan_cap_and_window_are_independent():
    r = ot.scan_texture_streaming(max_assets=500, top=3, offset=10)
    assert r["cap"] == 3 and r["sampling"]["window"] == 500 and r["sampling"]["offset"] == 10


def test_write_ops_require_double_key_at_domain_level():
    for fn in (ot.opt_texture_set_virtual, ot.opt_texture_clamp_max_size,
               ot.opt_texture_set_compression):
        env = fn(paths="/Game/T/X", dry_run=False, confirm=False)
        assert env["ok"] is False, fn.__name__
        assert env["error"]["code"] == envelope.Code.RUNTIME_ERROR
        assert "双钥" in env["error"]["message"]


def test_write_ops_default_dry_run_never_executes():
    for fn in (ot.opt_texture_set_virtual, ot.opt_texture_clamp_max_size,
               ot.opt_texture_set_compression):
        env = fn(paths="/Game/T/X")
        assert env["ok"] is True, fn.__name__
        res = env["result"]
        assert WRITE_KEYS <= set(res), sorted(res)
        assert res["dry_run"] is True and res["executed"] is False
        assert res["changed"] == 0 and res["planned"] == 0
        assert res["note"] == "no_engine"


def test_string_bools_from_bus_are_normalized_not_truthy():
    # 总线/MCP 可能把 bool 送成字符串："false" 绝不能被执行
    env = ot.opt_texture_clamp_max_size(paths="/Game/T/X", max_size=1024,
                                        dry_run="false", confirm="false")
    assert env["ok"] is False and "双钥" in env["error"]["message"]
    env2 = ot.opt_texture_clamp_max_size(paths="/Game/T/X", max_size=1024,
                                         dry_run="False", confirm="true")
    assert env2["ok"] is True and env2["result"]["dry_run"] is False
    assert env2["result"]["executed"] is False and env2["result"]["note"] == "no_engine"


def test_window_rows_put_actionable_first():
    """计划窗口排序纯逻辑：change 在前（收益降序），no_op 垫底。

    真机踩点：200 张窗口有 61 张待钳，但按体量降序返回的前 80 行几乎全是
    no_op，看起来像"没东西可做"——返回窗口必须先看得到要动的。
    """
    from prism.domain import optutil as ou
    rows = [{"kind": "no_op", "package": "/Game/A"},
            {"kind": "change", "savings_mb": 3.0, "package": "/Game/B"},
            {"kind": "failed", "package": "/Game/C"},
            {"kind": "change", "savings_mb": 40.0, "package": "/Game/D"}]
    ordered = sorted(rows, key=ou._row_order)
    assert [r["package"] for r in ordered] == ["/Game/D", "/Game/B", "/Game/C", "/Game/A"]
    assert ou._int_cap(0) == 0 and ou._int_cap("abc") == 80 and ou._int_cap(-5) == 0


def test_list_cap_is_reported_and_zero_means_untruncated():
    assert ot.opt_texture_clamp_max_size(paths="/Game/T/X")["result"]["list_cap"] == 80
    res = ot.opt_texture_clamp_max_size(paths="/Game/T/X", list_cap=0)["result"]
    assert res["list_cap"] == 0 and res["targets_truncated"] in (False, None)
    for fn, kw in ((ot.opt_texture_set_virtual, {}),
                   (ot.opt_texture_set_compression, {"compression": "default"})):
        assert fn(paths="/Game/T/X", list_cap=120, **kw)["result"]["list_cap"] == 120


def test_clamp_rejects_negative_max_size():
    env = ot.opt_texture_clamp_max_size(paths="/Game/T/X", max_size=-1)
    assert env["ok"] is False and env["error"]["code"] == envelope.Code.RUNTIME_ERROR


def test_advice_text_names_action_and_numbers():
    e = {"actions": ["clamp_max_size", "enable_vt"], "clamp_to": 2048,
         "clamp_savings_mb": 6.0, "vt_savings_mb": 8.0, "compression_savings_mb": 0.0,
         "virtual_texture": False, "never_stream": False, "vt_reason": None}
    text = ot._advice_for(e)
    assert "2048" in text and "虚拟纹理" in text
    e2 = dict(e, actions=[], virtual_texture=True, vt_savings_mb=0.0)
    assert "已是虚拟纹理" in ot._advice_for(e2)
    e3 = dict(e, actions=[], virtual_texture=False, never_stream=True)
    assert "never_stream" in ot._advice_for(e3)


def test_advice_text_splits_uncompressed_from_no_stream():
    """两类"非工程格式"的账不同：未压缩省 3/4 池；EditorIcon 系释放的是恒常驻。"""
    unc = {"actions": ["set_compression"], "clamp_to": None, "clamp_savings_mb": 0.0,
           "vt_savings_mb": 0.0, "compression_savings_mb": 12.0, "virtual_texture": False,
           "never_stream": False, "vt_reason": None, "uncompressed": True,
           "non_streaming_format": False, "permanent_mb": 0.0, "compression": "TC_NONE"}
    text = ot._advice_for(unc)
    assert "4B/px" in text and "12.0" in text and "恒常驻" not in text
    icon = {"actions": ["set_compression"], "clamp_to": None, "clamp_savings_mb": 0.0,
            "vt_savings_mb": 0.0, "compression_savings_mb": 0.0, "virtual_texture": False,
            "never_stream": False, "vt_reason": None, "uncompressed": False,
            "non_streaming_format": True, "permanent_mb": 8.0,
            "compression": "TextureCompressionSettings.TC_EDITORICON"}
    text2 = ot._advice_for(icon)
    assert "恒常驻" in text2 and "8.0" in text2 and "TC_Default" in text2
    assert "省约 0.0MB" not in text2        # 回归：别给 EditorIcon 报个 0 收益


def test_advice_text_high_bpp_names_8bpp_and_art_decision():
    """真机新增档：TC_HDR 的账必须写清"实测 8B/px + 改格式会丢动态范围"，
    而不是像未压缩系那样直接建议改 TC_Default。"""
    hdr = {"actions": [], "clamp_to": None, "clamp_savings_mb": 0.0, "vt_savings_mb": 0.0,
           "compression_savings_mb": 168.0, "virtual_texture": False, "never_stream": False,
           "vt_reason": None, "uncompressed": False, "non_streaming_format": False,
           "high_bpp": True, "runtime_mb": 192.0, "runtime_bytes_per_texel": 8.0,
           "permanent_mb": 0.0, "compression": "TextureCompressionSettings.TC_HDR"}
    text = ot._advice_for(hdr)
    assert "8B/px" in text and "192.0" in text and "168.0" in text
    assert "only_uncompressed=False" in text and "动态范围" in text
    assert "4B/px" not in text                 # 别把高位深当未压缩系


def test_compression_gate_leaves_high_bpp_and_unknown_alone():
    """only_uncompressed=True 的闸门：compressed/high_bpp/unknown 全 no_op，
    只有真未压缩与禁流送系放行（改错格式会毁观感，判不准就不动）。"""
    gate = ot._compression_gate
    for fmt in ("compressed", "high_bpp", "unknown"):
        d = gate(fmt, True, "TextureCompressionSettings.TC_HDR")
        assert d["kind"] == "no_op" and d["after"] == d["before"], fmt
    assert gate("high_bpp", True, "TC_HDR")["reason"].count("8B/px") == 1
    assert gate("compressed", True, "TC_DEFAULT")["reason"].count("only_uncompressed") == 1
    for fmt in ("uncompressed", "no_stream"):
        assert gate(fmt, True, "TC_NONE") is None, fmt
    for fmt in ("compressed", "high_bpp", "unknown", "uncompressed"):
        assert gate(fmt, False, "TC_HDR") is None, fmt
    assert gate("high_bpp", "false", "TC_HDR") is None   # 总线字符串同判


def test_compression_name_resolution_aliases_offline():
    # 无引擎时枚举不存在 -> 解析失败要诚实返回 None（写入时记 failed，不静默跳过）
    value, sig, tried = ot._resolve_compression(_NoUnreal(), "Default")
    assert value is None and sig is None and tried


def test_compression_gate_no_op_when_already_target():
    """5.4 真机 dry-run 暴露（缺陷 #10）：only_uncompressed=False 时，"本来就是目标格式"
    的贴图也被算成待改项——虚报 planned/收益，还会白白保存资产。"""
    gate = ot._compression_gate
    cur = "<TextureCompressionSettings.TC_DEFAULT: 0>"
    d = gate("compressed", False, cur, cur)
    assert d["kind"] == "no_op" and d["before"] == d["after"]
    assert "savings_mb" not in d            # no_op 不报价，避免 planned/收益双虚高
    assert "已是目标压缩格式" in d["reason"]
    # 同名判定优先于 only_uncompressed 闸门（否则同一格式会被报成"格式无法判定"之类）
    hdr = "<TextureCompressionSettings.TC_HDR: 6>"
    assert gate("high_bpp", True, hdr, hdr)["reason"].count("无需改写") == 1
    # 目标不同 -> 旧语义照常：False 放行、True 拦
    assert gate("high_bpp", False, cur, "<TextureCompressionSettings.TC_NORMAL: 1>") is None
    assert gate("high_bpp", True, cur, "<TextureCompressionSettings.TC_NORMAL: 1>")["kind"] == "no_op"
    # 解析不出成员名的文本不参与同名判定，别把本地化/怪形态误判成"已是目标"
    assert gate("uncompressed", False, u"未压缩", "weird") is None
    assert gate("unknown", True, None, cur)["kind"] == "no_op"      # 读不到格式仍保守


def test_enum_member_extracts_names_across_str_forms():
    em = ot._enum_member
    assert em("<TextureCompressionSettings.TC_MAX: 25>") == "TC_MAX"
    assert em("TextureCompressionSettings.TC_DEFAULT") == "TC_DEFAULT"
    assert em("TC_EDITOR_ICON") == "TC_EDITOR_ICON"
    assert em(None) is None and em("") is None and em("   ") is None


class _NoUnreal(object):
    """没有任何枚举成员的假 unreal 模块。"""



# ---------- 钳 MaxSize 判定（5.4 真机抽检暴露的两处口径错误）----------


def _entry_for_clamp(source_width=None, effective_width=None, max_size=None, runtime_mb=8.0):
    return {"source_width": source_width, "effective_width": effective_width,
            "max_size": max_size, "runtime_mb": runtime_mb}


def test_clamp_never_widens_a_tighter_existing_max_size():
    """真机回归：/Game/AutomotiveMaterials/Textures/Wheel/T_Dirt 现有 MaxSize=512，
    旧实现按源尺寸判定，把它计划成 512 -> 2048（把限制放大，不是优化）。"""
    d = ot._clamp_decision(_entry_for_clamp(source_width=8192, max_size=512), 2048)
    assert d["kind"] == "no_op" and d["after"] == 512
    assert "放宽" in d["reason"]
    assert d.get("savings_mb") is None


def test_clamp_reports_real_savings_not_zero():
    """真机回归：dry-run 曾恒报 savings_mb=0.0，而同资产 scan 报 79.8MB。"""
    d = ot._clamp_decision(_entry_for_clamp(source_width=4096, runtime_mb=8.0), 2048)
    assert d["kind"] == "change" and d["after"] == 2048
    assert d["savings_mb"] == 6.0          # 面积 1/4 -> 省 3/4（与 scan 的 clamp_savings_mb 同源）


def test_clamp_zero_max_size_is_a_cost_not_a_saving():
    d = ot._clamp_decision(_entry_for_clamp(source_width=4096, max_size=1024), 0, widen=True)
    assert d["kind"] == "change" and d["after"] == 0
    assert d["savings_mb"] < 0             # 取消限制 = 运行时变大，负数如实标出


def test_clamp_zero_on_uncapped_texture_is_no_op():
    d = ot._clamp_decision(_entry_for_clamp(source_width=4096, max_size=0), 0, widen=True)
    assert d["kind"] == "no_op" and d["after"] is None


def test_clamp_same_value_is_no_op():
    d = ot._clamp_decision(_entry_for_clamp(source_width=4096, max_size=2048), 2048)
    assert d["kind"] == "no_op" and d["reason"] == "max_size 已是 2048"


def test_clamp_without_any_size_is_unknown_not_change():
    d = ot._clamp_decision(_entry_for_clamp(source_width=0, effective_width=0, max_size=0), 2048)
    assert d["kind"] == "unknown" and d["note"]


def test_clamp_force_mode_allows_widen_with_visible_cost():
    d = ot._clamp_decision(_entry_for_clamp(source_width=8192, max_size=512), 2048,
                           only_if_larger=False)
    assert d["kind"] == "change" and d["savings_mb"] < 0
    assert "only_if_larger=False" in d["reason"]
