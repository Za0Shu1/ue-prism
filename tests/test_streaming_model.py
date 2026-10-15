"""v1.5 流送池模型单测（纯逻辑，无引擎）：分类 / 收益 / 汇总 / 池对照 / ini 解析。

这是"估算口径"的契约测试：opt_ 工具与 get_perf_report 共用 prism/streaming.py，
所以这里锁死数字，避免真机上改一处口径把两套账拆开。
"""
from __future__ import annotations

from prism import streaming as st


def _w(tmp_path, rel, text):
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


# ---------- 尺寸建议 ----------

def test_clamp_suggestion_uses_powers_of_two():
    assert st.next_pow2_floor(3000) == 2048
    assert st.clamp_suggestion(11585, 2048) == 2048   # 32K 级非压缩大图的典型靶
    assert st.clamp_suggestion(8192, 4096) == 4096
    assert st.clamp_suggestion(2048, 2048) is None     # 已在阈值内不建议动
    assert st.clamp_suggestion(1024, 0) is None        # 无阈值不猜
    assert st.clamp_suggestion(0, 2048) is None


# ---------- VT 可行性 ----------

def test_vt_support_blocks_cube_and_volume():
    assert st.vt_support("Texture2D")[0] is True
    assert st.vt_support("Texture2DArray")[0] is True
    for cls in ("TextureCube", "VolumeTexture"):
        ok, reason = st.vt_support(cls)
        assert ok is False and "不支持" in reason
    assert st.vt_support(None)[0] is True   # 类名缺失不武断，交由编辑器判定


# ---------- 压缩格式判定 ----------

def test_format_class_separates_four_runtime_cost_buckets():
    """四类格式物理后果不同：工程压缩(1B/px) / 未压缩(4B/px) / 高位深(8B/px) / 无 mip 禁流送(恒常驻)。"""
    for good in ("TextureCompressionSettings.TC_DEFAULT",
                 "unreal.TextureCompressionSettings.TC_NORMAL",
                 "TextureCompressionSettings.TC_MASKS",
                 "TextureCompressionSettings.TC_NORMALMAP"):
        assert st.format_class(good) == "compressed", good
        assert st.is_uncompressed(good) is False, good
        assert st.runtime_ratio(good) == 1.0, good
    for raw in ("TextureCompressionSettings.TC_NONE",
                "unreal.TextureCompressionSettings.TC_NONE",
                "TextureCompressionSettings.NONE", "TC_RGBA32", "SomeUncompressedThing"):
        assert st.format_class(raw) == "uncompressed", raw
    for ns in ("TextureCompressionSettings.TC_EDITORICON",
               "TextureCompressionSettings.TC_EDITOR_ICON",   # 下划线写法同判
               "unreal.TextureCompressionSettings.TC_EDITORICON",
               "TextureCompressionSettings.TC_MAX"):
        assert st.format_class(ns) == "no_stream", ns
        # 关键回归：EditorIcon 不是"4B/px 未压缩"，别按未压缩虚报 3/4 收益
        assert st.is_uncompressed(ns) is False, ns
    assert st.format_class(None) == "unknown"
    assert st.format_class("") == "unknown"
    assert st.format_class(u"编辑器图标") == "unknown"    # 本地化文本不误判
    assert st.is_uncompressed(None) is False
    assert st.is_uncompressed("") is False
    # 5.4 真机定案：TC_HDR 名义是"压缩设置"，实测并不降块压缩 -> 单列 high_bpp
    assert st.format_class("TextureCompressionSettings.TC_HDR") == "high_bpp"
    assert st.format_class("unreal.TextureCompressionSettings.TC_HDR") == "high_bpp"
    assert st.is_high_bpp("TextureCompressionSettings.TC_HDR") is True
    assert st.is_high_bpp("TextureCompressionSettings.TC_NONE") is False
    assert st.is_high_bpp(None) is False
    assert st.is_uncompressed("TextureCompressionSettings.TC_HDR") is False


# ---------- 运行时台账（TC_HDR 真机数字定案）----------

def test_runtime_ratio_matches_format_bucket():
    """倍率表：BC 1 / 未压缩 4 / 高位深 8；判不准(unknown)不放大，宁保守不误报。"""
    assert st.runtime_ratio("TextureCompressionSettings.TC_DEFAULT") == 1.0
    assert st.runtime_ratio("TextureCompressionSettings.TC_NONE") == 4.0
    assert st.runtime_ratio("TextureCompressionSettings.TC_HDR") == 8.0
    assert st.runtime_ratio("TextureCompressionSettings.TC_EDITORICON") == 1.0
    assert st.runtime_ratio(u"编辑器图标") == 1.0
    assert st.runtime_ratio(None) == 1.0


def test_runtime_ledger_trusts_measured_once():
    """真机 /Game/ClientLib/Material/Textures/T_HDR：memory_bytes=201326592=6x2048^2x8B。
    引擎数已含真实格式，采信后不得再乘倍率（旧的 x4/x8 兜底会把它报成 GB 级假账）。"""
    led = st.runtime_ledger({"est_runtime_bytes": 33470545,
                             "memory_bytes": 201326592,
                             "compression": "TextureCompressionSettings.TC_HDR"})
    assert led["runtime_bytes"] == 201326592
    assert led["bc_equivalent_bytes"] == 25165824
    assert led["runtime_ratio"] == 8.0
    assert led["runtime_bytes_per_texel"] == 8.0
    assert led["runtime_basis"] == "resident_memory"


def test_runtime_ledger_falls_back_to_estimate_when_not_uploaded():
    """冷态实测只剩占位（真机 T_HDR_NIGHT02 未上传=24576B）：按 BC 近似 x 倍率兜底。"""
    led = st.runtime_ledger({"est_runtime_bytes": 4 * 1048576, "memory_bytes": 4096,
                             "compression": "TextureCompressionSettings.TC_HDR"})
    assert led["runtime_basis"] == "derived_estimate"
    assert led["runtime_bytes"] == 32 * 1048576
    assert led["bc_equivalent_bytes"] == 4 * 1048576
    empty = st.runtime_ledger({})
    assert empty["runtime_bytes"] == 0.0 and empty["bc_equivalent_bytes"] == 0.0
    assert empty["runtime_basis"] == "derived_estimate"


# ---------- 单张池条目 ----------

BIG = 8 * 1048576


def test_pool_entry_routes_streamed_permanent_virtual():
    metrics = {"est_runtime_bytes": BIG, "source_width": 4096}
    streamed = st.texture_pool_entry(metrics, "Texture2D")
    assert streamed["pool_mb"] == 8.0 and streamed["permanent_mb"] == 0.0
    never = st.texture_pool_entry(dict(metrics, never_stream=True), "Texture2D")
    assert never["pool_mb"] == 0.0 and never["permanent_mb"] == 8.0
    assert "not_in_pool" in never["flags"]
    virt = st.texture_pool_entry(dict(metrics, virtual_texture=True), "Texture2D")
    assert virt["pool_mb"] == 0.0 and virt["virtual_mb"] == 8.0
    assert virt["actions"] == []          # 已虚拟化不再建议开 VT
    cube = st.texture_pool_entry(metrics, "TextureCube")
    assert cube["vt_supported"] is False and "vt_unsupported" in cube["flags"]
    assert cube["vt_savings_mb"] == 0.0 and cube["pool_mb"] == 8.0


def test_pool_entry_clamp_savings_math():
    e = st.texture_pool_entry({"est_runtime_bytes": BIG, "source_width": 4096}, "Texture2D")
    assert e["clamp_to"] == 2048
    assert e["clamp_savings_mb"] == 6.0          # 4096->2048 面积 1/4 -> 省 3/4
    assert "clamp_max_size" in e["actions"] and "enable_vt" in e["actions"]
    assert "oversized_mip_chain" in e["flags"]


def test_pool_entry_max_size_capped_flag_and_no_double_clamp():
    e = st.texture_pool_entry({"est_runtime_bytes": 1024, "source_width": 16384,
                               "effective_width": 512, "max_size": 512}, "Texture2D")
    assert "max_size_capped" in e["flags"]
    assert e["clamp_to"] is None and "clamp_max_size" not in e["actions"]
    assert e["runtime_mb"] == 0.001


def test_pool_entry_uncompressed_inflates_and_suggests_compression():
    e = st.texture_pool_entry({"est_runtime_bytes": 4 * 1048576, "source_width": 2048,
                               "compression": "TextureCompressionSettings.TC_NONE"},
                              "Texture2D", px_warn=2048)
    assert e["format_class"] == "uncompressed"
    assert e["uncompressed"] is True
    assert e["runtime_mb"] == 16.0              # BC 近似 4MB -> 实际按 4B/px 约 16MB
    assert e["bc_equivalent_mb"] == 4.0
    assert e["uncompressed_mb"] == 16.0
    assert e["compression_savings_mb"] == 12.0
    assert e["pool_mb"] == 16.0                 # 未压缩系照样进池
    assert e["streaming_release_mb"] == 0.0
    assert "set_compression" in e["actions"]


def test_pool_entry_no_stream_format_is_permanent_not_pool():
    """EditorIcon 系：无 mip+禁流送 -> 整张恒常驻池外；VT 建议对它不成立，钳尺寸仍有效。"""
    e = st.texture_pool_entry({"est_runtime_bytes": BIG, "source_width": 4096,
                               "compression": "TextureCompressionSettings.TC_EDITORICON"},
                              "Texture2D")
    assert e["format_class"] == "no_stream"
    assert e["uncompressed"] is False and e["non_streaming_format"] is True
    assert e["pool_mb"] == 0.0 and e["permanent_mb"] == 8.0 and e["virtual_mb"] == 0.0
    assert "non_streaming_format" in e["flags"] and "non_streaming_format_permanent" in e["flags"]
    assert e["vt_savings_mb"] == 0.0 and "enable_vt" not in e["actions"]   # 池外，开 VT 无从谈起
    assert e["compression_savings_mb"] == 0.0            # 不是 4B/px 的账
    assert e["streaming_release_mb"] == 8.0              # 收益=改回 TC_Default 变可流送
    assert e["clamp_to"] == 2048 and e["clamp_savings_mb"] == 6.0   # 常驻也能靠钳尺寸减
    assert e["actions"] == ["set_compression", "clamp_max_size"]


def test_pool_entry_high_bpp_is_measured_not_remultiplied():
    """真机回归：TC_HDR 曾被归"压缩"按 1B/px 估，192MB 立方图算成 31.9MB（低估 6 倍）。
    采信实测后不再乘倍率；改格式的收益要报，但不进 actions（会丢 HDR 动态范围）。"""
    hdr = {"est_runtime_bytes": 33470545, "memory_bytes": 201326592,
           "source_width": 2048, "compression": "TextureCompressionSettings.TC_HDR"}
    e = st.texture_pool_entry(hdr, "TextureCube")
    assert e["format_class"] == "high_bpp" and e["high_bpp"] is True
    assert e["runtime_mb"] == 192.0 and e["bc_equivalent_mb"] == 24.0
    assert e["runtime_ratio"] == 8.0 and e["runtime_bytes_per_texel"] == 8.0
    assert e["runtime_basis"] == "resident_memory"
    assert e["pool_mb"] == 192.0                      # 立方图也是真池大户
    assert e["uncompressed"] is False and e["uncompressed_mb"] == 0.0
    assert e["compression_savings_mb"] == 168.0       # 192 - 24（折成 BC 的下界）
    assert "high_bpp_format" in e["flags"]
    assert "set_compression" not in e["actions"]
    assert e["vt_savings_mb"] == 0.0 and "vt_unsupported" in e["flags"]
    assert e["actions"] == []                         # 只报账，不擅自建议改美术格式


def test_pool_entry_high_bpp_cold_upload_scales_estimate():
    """未上传时按 BC 近似 x8 兜底，别再回到 1B/px 的低估口径。"""
    e = st.texture_pool_entry({"est_runtime_bytes": 4 * 1048576, "memory_bytes": 4096,
                               "source_width": 1024,
                               "compression": "TextureCompressionSettings.TC_HDR"}, "Texture2D")
    assert e["runtime_basis"] == "derived_estimate"
    assert e["runtime_mb"] == 32.0 and e["compression_savings_mb"] == 28.0


def test_pool_entry_no_metrics_is_not_a_finding():
    e = st.texture_pool_entry({}, "Texture2D")
    assert e["runtime_mb"] == 0.0 and "no_runtime_metrics" in e["flags"]
    assert e["actions"] == []


def test_pool_entry_thresholds_are_per_target():
    metrics = {"est_runtime_bytes": BIG, "source_width": 2048}
    pc = st.texture_pool_entry(metrics, "Texture2D", px_warn=2048)
    mobile = st.texture_pool_entry(metrics, "Texture2D", px_warn=1024, px_error=2048)
    assert pc["clamp_to"] is None
    assert mobile["clamp_to"] == 1024
    assert "oversized_mip_chain" in mobile["flags"]


def test_pool_entry_small_texture_gets_no_advice():
    e = st.texture_pool_entry({"est_runtime_bytes": 64 * 1024, "source_width": 256}, "Texture2D")
    assert e["actions"] == [] and e["vt_savings_mb"] == 0.0    # 建议要有收益门槛，否则全是噪音


def test_format_hint_tables_are_normalized():
    """提示表若含下划线/空格 = 永远匹配不上的死条目（_norm_format 已抹平），当场钉死。"""
    for table in (st.NO_STREAM_FORMATS, st.UNCOMPRESSED_FORMATS, st.COMPRESSED_HINTS,
                  st.HIGH_BPP_FORMATS, st.VT_UNSUPPORTED_HINTS):
        for hint in table:
            assert hint == hint.lower(), hint
            assert "_" not in hint and " " not in hint, hint


# ---------- 汇总（防把同一 MB 算三遍）----------

def _entry(pkg, **kw):
    base = {"package": pkg, "runtime_mb": 0.0, "pool_mb": 0.0, "permanent_mb": 0.0,
            "virtual_mb": 0.0, "clamp_savings_mb": 0.0, "vt_savings_mb": 0.0,
            "compression_savings_mb": 0.0, "actions": [], "flags": [],
            "uncompressed": False, "never_stream": False, "vt_supported": True}
    base.update(kw)
    return base


def test_aggregate_counts_and_caps_release_by_demand():
    entries = [
        _entry("/Game/A", runtime_mb=10.0, pool_mb=10.0, clamp_savings_mb=6.0,
               vt_savings_mb=10.0, actions=["clamp_max_size", "enable_vt"],
               flags=["oversized_mip_chain"]),
        _entry("/Game/B", runtime_mb=4.0, permanent_mb=4.0, never_stream=True,
               flags=["never_stream_permanent", "not_in_pool"]),
        _entry("/Game/C", flags=["no_runtime_metrics"]),
    ]
    agg = st.aggregate(entries)
    assert agg["textures"] == 3 and agg["measured"] == 2 and agg["unmeasured"] == 1
    assert agg["pool_demand_mb"] == 10.0 and agg["permanent_mb"] == 4.0
    assert agg["need_clamp_count"] == 1 and agg["need_vt_count"] == 1
    assert agg["oversized_count"] == 1 and agg["never_stream_count"] == 1
    # clamp(6)+vt(10)=16 但池需求只有 10 -> 合并值封顶，不虚报
    assert agg["potential_pool_release_mb"] == 10.0
    assert agg["model"]["version"] == st.POOL_MODEL_VERSION


def test_aggregate_keeps_permanent_and_pool_ledgers_separate():
    """常驻释放与池释放分列：同一 MB 不得既算池收益又算常驻收益。"""
    entries = [
        _entry("/Game/A", runtime_mb=10.0, pool_mb=10.0, vt_savings_mb=10.0,
               actions=["enable_vt"]),
        _entry("/Game/Icon", runtime_mb=8.0, permanent_mb=8.0, non_streaming_format=True,
               streaming_release_mb=8.0, format_class="no_stream",
               actions=["set_compression", "clamp_max_size"], clamp_savings_mb=6.0),
    ]
    agg = st.aggregate(entries)
    assert agg["pool_demand_mb"] == 10.0 and agg["permanent_mb"] == 8.0
    assert agg["non_streaming_count"] == 1 and agg["uncompressed_count"] == 0
    assert agg["streaming_release_mb"] == 8.0
    assert agg["potential_permanent_release_mb"] == 8.0
    assert agg["potential_pool_release_mb"] == 10.0      # 只算池内那张
    assert agg["clamp_savings_mb"] == 6.0                # 钳尺寸单列，不并进池收益之外重复计


def test_aggregate_empty_is_all_zero_not_none():
    agg = st.aggregate([])
    assert agg["textures"] == 0 and agg["pool_demand_mb"] == 0.0
    assert agg["potential_pool_release_mb"] == 0.0


def test_aggregate_counts_high_bpp_separately_from_uncompressed():
    """high_bpp 单列计数：不能和未压缩系混成一档（收益比例 7/8 vs 3/4，动作也不同）。"""
    agg = st.aggregate([
        _entry("/Game/H", runtime_mb=192.0, pool_mb=192.0, high_bpp=True,
               compression_savings_mb=168.0, format_class="high_bpp",
               flags=["high_bpp_format"]),
        _entry("/Game/U", runtime_mb=16.0, pool_mb=16.0, uncompressed=True,
               compression_savings_mb=12.0, actions=["set_compression"]),
    ])
    assert agg["high_bpp_count"] == 1 and agg["uncompressed_count"] == 1
    assert agg["pool_demand_mb"] == 208.0
    assert agg["compression_savings_mb"] == 180.0     # 单列，不并进池收益封顶
    assert agg["potential_pool_release_mb"] == 0.0


# ---------- 池对照 ----------

def test_compare_pool_verdicts():
    assert st.compare_pool(2000, 1000)["verdict"] == "overflow"
    assert st.compare_pool(2000, 1000)["overflow_mb"] == 1000.0
    assert st.compare_pool(850, 1000)["verdict"] == "tight"
    assert st.compare_pool(100, 1000)["verdict"] == "ok"
    assert st.compare_pool(10, 20)["verdict"] == "ok"           # 未过关注线不算 tight
    assert st.compare_pool(100, None)["verdict"] == "unknown"    # 读不到池就不下超限结论
    assert st.compare_pool(100, 0)["verdict"] == "disabled"
    assert st.compare_pool(100, -1)["verdict"] == "disabled"


# ---------- ini 解析 ----------

def test_read_pool_config_prefers_platform_over_cvar_over_scalability(tmp_path):
    _w(tmp_path, "Config/DefaultScalability.ini",
       "[StreamingQualitySettings 0]\nsg.Streaming.PoolSize=2048\n")
    _w(tmp_path, "Config/DefaultEngine.ini",
       "[ConsoleVariables]\n; comment\nr.Streaming.PoolSize=4096\n")
    _w(tmp_path, "Config/Windows/WindowsEngine.ini",
       "[/Script/WindowsTargetPlatform.WindowsTargetSettings]\nr.Streaming.PoolSize=8192\n")
    cfg = st.read_pool_config(str(tmp_path))
    assert cfg["effective_mb"] == 8192.0
    assert cfg["effective"]["kind"] == "platform"
    assert {c["kind"] for c in cfg["pool_candidates"]} == {"platform", "cvar", "scalability"}
    assert cfg["files_scanned"] == 3


def test_read_pool_config_takes_biggest_within_same_kind(tmp_path):
    _w(tmp_path, "Config/DefaultEngine.ini",
       "[ConsoleVariables]\nr.Streaming.PoolSize=1024\n[Other]\nr.Streaming.PoolSize=2048\n")
    assert st.read_pool_config(str(tmp_path))["effective_mb"] == 2048.0


def test_read_pool_config_missing_is_honest(tmp_path):
    _w(tmp_path, "Config/DefaultEngine.ini", "[Rendering]\nbEnableVsync=False\n")
    cfg = st.read_pool_config(str(tmp_path))
    assert cfg["effective_mb"] is None and cfg["pool_candidates"] == []
    assert "未发现任何 *PoolSize" in cfg["note"]
    assert st.compare_pool(500.0, cfg["effective_mb"])["verdict"] == "unknown"


def test_read_pool_config_without_config_dir(tmp_path):
    cfg = st.read_pool_config(str(tmp_path / "nope"))
    assert cfg["effective_mb"] is None and "无 Config 目录" in cfg["note"]


def test_vt_enabled_reads_renderer_flags(tmp_path):
    _w(tmp_path, "Config/DefaultEngine.ini",
       "[/Script/Engine.RendererSettings]\nbEnableVirtualTextureSupport=False\n")
    assert st.vt_enabled(st.read_pool_config(str(tmp_path))) is False
    _w(tmp_path, "Config/DefaultEngine.ini", "[ConsoleVariables]\nr.VirtualTextures=True\n")
    assert st.vt_enabled(st.read_pool_config(str(tmp_path))) is True
    _w(tmp_path, "Config/DefaultEngine.ini", "[Rendering]\nbEnableVsync=False\n")
    assert st.vt_enabled(st.read_pool_config(str(tmp_path))) is None   # 查不到=未知，不猜


# ---------- 源尺寸反推：候选 BPP x 面数（5.4 真机假收益的根因）----------


def test_source_side_picks_pow2_bpp_not_blind_rgba8():
    """T_HDR(TextureCube) 源内存 201326592 = 6 x 2048^2 x 8B(RGBA16)。
    一律按 4B/px 反推会得出 2896 的假尺寸，进而编出"钳到 2048 省 96MB"的假收益。"""
    assert st.source_side_from_memory(2048 * 2048 * 6 * 8, 6) == (2048, 8, 6, True)
    assert st.source_side_from_memory(8192 * 8192 * 8, 1)[:2] == (8192, 8)   # T_Dirt 型
    assert st.source_side_from_memory(4096 * 4096 * 4, 1)[:3] == (4096, 4, 1)  # 常规仍优先 4B/px


def test_source_side_falls_back_and_marks_inexact():
    side, bpp, _faces, exact = st.source_side_from_memory(3000 * 3000 * 4, 1)
    assert (side, bpp, exact) == (3000, 4, False)      # 非 2 的幂：如实标 exact=False
    for bad in (None, 0, -1, "abc"):
        assert st.source_side_from_memory(bad, 1)[0] == 0, bad


def test_faces_for_cube_and_arrays():
    assert st.faces_for("TextureCube") == 6
    assert st.faces_for("Texture2D") == 1
    assert st.faces_for("Texture2DArray", 4) == 4
    assert st.faces_for("TextureCube", 2) == 12
    assert st.faces_for(None, "x") == 1


def test_effective_px_is_the_tighter_of_source_and_max_size():
    assert st.effective_px(4096, 0) == 4096
    assert st.effective_px(4096, 512) == 512
    assert st.effective_px(0, 512) == 512
    assert st.effective_px(0, 0) == 0


def test_resize_delta_mb_sign_follows_size_direction():
    assert st.resize_delta_mb(8.0, 4096, 2048) == 6.0        # 收紧 = 正收益
    assert st.resize_delta_mb(8.0, 4096, 4096) == 0.0
    assert st.resize_delta_mb(8.0, 1024, 4096) == -120.0     # 放宽 = 负数（代价）
    assert st.resize_delta_mb(8.0, 0, 2048) == 0.0
    assert st.resize_delta_mb(8.0, 4096, None) == 0.0


def test_pool_entry_does_not_double_clamp_when_max_is_looser_than_source():
    """现有 MaxSize 比源尺寸还大 = 运行时由源尺寸决定，别按 MaxSize 折算"省 3/4"。"""
    e = st.texture_pool_entry({"est_runtime_bytes": BIG, "source_width": 1024,
                               "max_size": 4096}, "Texture2D")
    assert e["clamp_to"] is None and e["clamp_savings_mb"] == 0.0
    assert "clamp_max_size" not in e["actions"]


def test_pool_entry_cube_at_threshold_gets_no_clamp_advice():
    """源尺寸就是 2048 的立方图（HDR 8B/px）不该被推成 2896 后建议"钳到 2048"。"""
    hdr = {"est_runtime_bytes": BIG, "source_width": 2048,
           "source_bytes_per_texel": 8, "compression": "TextureCompressionSettings.TC_HDR"}
    e = st.texture_pool_entry(hdr, "TextureCube", px_warn=2048)
    assert e["clamp_to"] is None and e["clamp_savings_mb"] == 0.0


# ---------- 安全阀：源边长反推不可信时撤金额、留发现（#11 A" ）----------


def _cold_cube():
    """真实工程复盘的冷态靶：4096 立方图(TC_HDR 8B/px) 未载入时被反推成 2365。"""
    return {"est_runtime_bytes": int(2365 * 2365 * 6 * 1.33), "memory_bytes": 4096,
            "source_width": 2365, "source_side_exact": False,
            "source_bytes_per_texel": 8,
            "compression": "TextureCompressionSettings.TC_HDR"}


def test_pool_entry_withholds_money_but_keeps_plan_when_side_unverified():
    e = st.texture_pool_entry(_cold_cube(), "TextureCube")
    assert e["size_basis"] == "source_unverified"
    assert e["source_side_exact"] is False
    assert "size_unverified" in e["flags"]
    assert e["clamp_to"] == 2048 and e["clamp_savings_mb"] is None
    assert "clamp_max_size" in e["actions"]        # 不报价 != 不建议
    assert e["compression_savings_mb"] is None     # bc 由边长算出 -> 一并撤回
    assert e["runtime_mb"] > 0.0 and e["pool_mb"] > 0.0   # 台账仍给（带 flag 的估计值）


def test_pool_entry_unverified_cold_withholds_vt_and_release_amounts():
    m = {"est_runtime_bytes": 4 * 1048576, "memory_bytes": 4096,
         "source_width": 2365, "source_side_exact": False,
         "compression": "TextureCompressionSettings.TC_NONE"}
    e = st.texture_pool_entry(m, "Texture2D")
    assert e["runtime_basis"] == "derived_estimate"
    assert e["vt_savings_mb"] is None and e["clamp_savings_mb"] is None
    assert e["compression_savings_mb"] is None
    assert set(e["actions"]) == {"set_compression", "enable_vt", "clamp_max_size"}
    icon = st.texture_pool_entry(dict(m, compression="TextureCompressionSettings.TC_EDITORICON"),
                                 "Texture2D")
    assert icon["streaming_release_mb"] is None and icon["permanent_mb"] == 4.0
    assert "set_compression" in icon["actions"]


def test_pool_entry_measured_memory_keeps_vt_and_release_quotes():
    """实测 MB 与边长无关：反推不可信时 vt/常驻释放照报，只有依赖边长的两档撤回。"""
    m = {"est_runtime_bytes": BIG, "memory_bytes": 201326592,
         "source_width": 2365, "source_side_exact": False,
         "compression": "TextureCompressionSettings.TC_EDITORICON"}
    e = st.texture_pool_entry(m, "Texture2D")
    assert e["runtime_basis"] == "resident_memory"
    assert e["permanent_mb"] == 192.0 and e["streaming_release_mb"] == 192.0
    assert e["clamp_to"] == 2048 and e["clamp_savings_mb"] is None


def test_pool_entry_exact_or_missing_flag_is_not_gated():
    """只有显式 false 触发安全阀：exact=True / 缺键（probe_source=False 等）照旧报价。"""
    exact = st.texture_pool_entry({"est_runtime_bytes": BIG, "source_width": 4096,
                                   "source_side_exact": True}, "Texture2D")
    assert exact["size_basis"] == "source_exact"
    assert exact["clamp_savings_mb"] == 6.0 and exact["vt_savings_mb"] == 8.0
    assert "size_unverified" not in exact["flags"]
    legacy = st.texture_pool_entry({"est_runtime_bytes": BIG, "source_width": 4096},
                                   "Texture2D")
    assert legacy["size_basis"] == "source_exact" and legacy["clamp_savings_mb"] == 6.0
    warm = st.texture_pool_entry({"est_runtime_bytes": BIG, "width": 4096}, "Texture2D")
    assert warm["size_basis"] == "runtime_px" and warm["clamp_savings_mb"] == 6.0
    assert st.texture_pool_entry({}, "Texture2D")["size_basis"] == "unknown"


def test_aggregate_reports_size_uncertainty_and_withheld_quotes():
    cold = st.texture_pool_entry(_cold_cube(), "TextureCube")
    warm = st.texture_pool_entry({"est_runtime_bytes": BIG, "source_width": 4096,
                                  "source_side_exact": True}, "Texture2D")
    icon = st.texture_pool_entry({"est_runtime_bytes": 4 * 1048576, "memory_bytes": 4096,
                                  "source_width": 2365, "source_side_exact": False,
                                  "compression": "TextureCompressionSettings.TC_EDITORICON"},
                                 "Texture2D")
    agg = st.aggregate([dict(cold, package="/Game/T/Cube"),
                        dict(warm, package="/Game/T/Warm"),
                        dict(icon, package="/Game/T/Icon")])
    assert agg["size_unverified_count"] == 2
    assert agg["size_unverified_pool_mb"] == round(cold["pool_mb"] + icon["pool_mb"], 3)
    assert agg["size_unverified_permanent_mb"] == icon["permanent_mb"]
    assert agg["savings_withheld_count"] == 2
    assert agg["clamp_savings_mb"] == warm["clamp_savings_mb"]   # None 按 0 计入，不猜数
    assert agg["model"]["version"] == "v3-size-verification-aware"
    assert "size_verification" in agg["model"]
