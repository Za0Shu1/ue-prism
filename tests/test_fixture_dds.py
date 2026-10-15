# -*- coding: utf-8 -*-
"""#11 B 已知真值靶（scripts/calib_fixture/make_dds.py）的离线用例。

两件事：
1) 靶本身必须可核验——解析 DDS 头并核对 payload == 边长^2 x BPT x 面数，
   否则"已知真值"只是自说自话。
2) 把 5.3.2 与 5.4.4 实测的 source_memory_bytes / 反推结果钉成常量（两版逐字节一致，
   由 probe_source_truth.py 复跑可验证），这样冷态歧义的现状数字不需要引擎也能回归。
   其中"伪精确仍照常报价"是**快照锁**：#11 口径（BPP 先验 / 面数语义 / C 直读）一改，
   这里必须一起改——金额口径不许悄悄漂。
"""
import importlib.util
import os
import struct

import pytest

from prism import streaming as st

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_MAKE_DDS = os.path.join(_REPO, "scripts", "calib_fixture", "make_dds.py")


def _load_make_dds():
    spec = importlib.util.spec_from_file_location("make_dds", _MAKE_DDS)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


dds = _load_make_dds()

# 5.3.2 / 5.4.4 实测（probe_source_truth.py，两版每个键都相同）
MEASURED = {
    "T_CalibF32": {"src_mem": 4194304, "faces_used": 1, "side": 1024, "bpt": 4, "exact": True,
                   "class": "Texture2D",
                   "compression": "TextureCompressionSettings.TC_HDR_COMPRESSED"},
    "T_CalibF16": {"src_mem": 2097152, "faces_used": 1, "side": 512, "bpt": 8, "exact": True,
                   "class": "Texture2D",
                   "compression": "TextureCompressionSettings.TC_HDR_COMPRESSED"},
    "T_CalibRGBA8": {"src_mem": 4194304, "faces_used": 1, "side": 1024, "bpt": 4, "exact": True,
                     "class": "Texture2D",
                     "compression": "TextureCompressionSettings.TC_DEFAULT"},
    "T_CalibR16": {"src_mem": 2097152, "faces_used": 1, "side": 512, "bpt": 8, "exact": True,
                   "class": "Texture2D",
                   "compression": "TextureCompressionSettings.TC_DEFAULT"},
    "T_CalibR8": {"src_mem": 1048576, "faces_used": 1, "side": 512, "bpt": 4, "exact": True,
                  "class": "Texture2D",
                  "compression": "TextureCompressionSettings.TC_DEFAULT"},
    # DDS 立方图被 5.3/5.4 导入成 Texture2DArray，且 array_size 全部尺寸 API miss
    # -> 产品侧 faces 记 1，而 source_memory 含 6 面 -> 反推 1254、非 2 的幂、安全阀生效
    "T_CalibCubeF32": {"src_mem": 6291456, "faces_used": 1, "side": 1254, "bpt": 4,
                       "exact": False, "class": "Texture2DArray",
                       "compression": "TextureCompressionSettings.TC_HDR_COMPRESSED"},
}

AUTHORED = {t[0]: (t[1], dds.FORMATS[t[3]][1], t[4]) for t in dds.TARGETS}
                                               # name -> (side, bpt, faces)


@pytest.fixture(scope="session")
def dds_files(tmp_path_factory):
    out = str(tmp_path_factory.mktemp("dds"))
    truth = dds.make_dds(out)
    return truth, out


def _parse(raw):
    assert raw[:4] == b"DDS "
    dwsize, flags, h, w, pitch, depth, mips = struct.unpack_from("<7I", raw, 4)
    pf_size, pf_flags = struct.unpack_from("<2I", raw, 76)
    fourcc = raw[84:88]
    caps, caps2 = struct.unpack_from("<2I", raw, 108)
    dxgi, rd, misc, arr, _mf2 = struct.unpack_from("<5I", raw, 128)
    return {"dwsize": dwsize, "flags": flags, "h": h, "w": w, "pitch": pitch,
            "depth": depth, "mips": mips, "pf_size": pf_size, "pf_flags": pf_flags,
            "fourcc": fourcc, "caps": caps, "caps2": caps2,
            "dxgi": dxgi, "rd": rd, "misc": misc, "arr": arr,
            "payload": len(raw) - 148}


def test_dds_layout_is_minimal_and_parseable(dds_files):
    truth, _out = dds_files
    for item in truth["targets"]:
        got = _parse(open(item["path"], "rb").read())
        assert got["dwsize"] == 124 and got["pf_size"] == 32
        assert got["fourcc"] == b"DX10" and got["pf_flags"] == dds.DDPF_FOURCC
        assert got["w"] == item["authored_width"] and got["h"] == item["authored_height"]
        assert got["mips"] == 1 and got["depth"] == 1        # 源位图不带 mip
        assert got["pitch"] == item["authored_width"] * item["authored_bpt"]


def test_dds_payload_equals_authored_truth(dds_files):
    truth, _out = dds_files
    for item in truth["targets"]:
        got = _parse(open(item["path"], "rb").read())
        side, bpt, faces = AUTHORED[item["name"]]
        assert got["payload"] == side * side * bpt * faces
        assert item["payload_bytes"] == got["payload"]


def test_cube_target_is_texture2d_dimension_with_cube_misc(dds_files):
    """5.4 实证：立方图不是 resourceDimension=4(那是 TEXTURE3D，引擎 Error=5 拒读)。"""
    truth, _out = dds_files
    by = {i["name"]: i for i in truth["targets"]}
    cube = _parse(open(by["T_CalibCubeF32"]["path"], "rb").read())
    flat = _parse(open(by["T_CalibF32"]["path"], "rb").read())
    assert cube["rd"] == 3 and cube["misc"] == dds.DX10_MISC_TEXTURECUBE and cube["arr"] == 6
    assert cube["caps2"] == dds.DDSCAPS2_CUBEMAP | dds.DDSCAPS2_CUBEMAP_ALL_FACES
    assert flat["rd"] == 3 and flat["misc"] == 0 and flat["arr"] == 1 and flat["caps2"] == 0


def test_target_set_covers_every_bpp_class(dds_files):
    """先验只认识 4/8 两档，靶必须把 16/8/4/2/1 全摆出来，歧义面才量得清。"""
    bpts = {bpt for _side, bpt, _f in AUTHORED.values()}
    assert bpts == {16, 8, 4, 2, 1}
    assert any(f == 6 for _s, _b, f in AUTHORED.values())
    truth, _out = dds_files
    assert len(truth["targets"]) == len(dds.TARGETS) == 6


def test_measured_source_memory_equals_authored_payload(dds_files):
    """引擎侧 source_memory_bytes 与我写的 payload 逐字节相等：UE 存的是原始位图。

    这条同时否掉"C 靠读 UE 存的源图头拿宽高"的乐观预期——存的是裸位图，
    PNG/DDS 头不在里面（float 类 src_disk == src_mem，定点类才多几 K 序列化开销）。
    """
    truth, _out = dds_files
    for item in truth["targets"]:
        _side, bpt, faces = AUTHORED[item["name"]]
        assert MEASURED[item["name"]]["src_mem"] == item["payload_bytes"]
        assert item["payload_bytes"] == _side * _side * bpt * faces


def test_cold_derivation_reproduces_engine_answers():
    for name, m in MEASURED.items():
        got = st.source_side_from_memory(m["src_mem"], m["faces_used"])
        assert got[:4] == (m["side"], m["bpt"], m["faces_used"], m["exact"]), name


def test_current_prior_gets_only_two_of_six_right():
    """文档里"现状 2/6 正确"的出处：不靠引擎也能复算。"""
    ok = [n for n, m in MEASURED.items() if m["side"] == AUTHORED[n][0]]
    assert sorted(ok) == ["T_CalibF16", "T_CalibRGBA8"]


def test_pseudo_exact_cases_still_quote_money_snapshot_lock():
    """伪精确(exact=True 但边长错)不在 A″ 覆盖面上：金额照旧报。

    快照锁——口径改了必须同步改这里，金额口径不许悄悄漂。
    """
    wrong = [n for n, m in MEASURED.items()
             if m["exact"] and m["side"] != AUTHORED[n][0]]
    assert sorted(wrong) == ["T_CalibF32", "T_CalibR16", "T_CalibR8"]
    for name in wrong:
        m = MEASURED[name]
        entry = st.texture_pool_entry(
            {"est_runtime_bytes": m["side"] * m["side"] * m["bpt"],
             "source_width": m["side"], "source_side_exact": True,
             "max_size": 512, "compression": m["compression"]}, m["class"])
        assert entry["size_basis"] == "source_exact", name
        assert entry["clamp_savings_mb"] is not None, name
        assert "size_unverified" not in entry["flags"], name


def test_multiface_array_case_withholds_side_dependent_money():
    """DDS 立方图源被 5.3/5.4 读成 Texture2DArray：faces 记 1 而 source_memory 含 6 面 -> 1254 无可信解。

    该靶在钳尺寸阈值(2048)以下，本来就没有 clamp 候选；安全阀要撤的是"依赖边长的金额"
    （无实测时 VT 那档置 None），发现与动作一律保留。
    """
    m = MEASURED["T_CalibCubeF32"]
    entry = st.texture_pool_entry(
        {"est_runtime_bytes": m["side"] * m["side"] * m["bpt"],
         "source_width": m["side"], "source_side_exact": False,
         "max_size": 256, "compression": m["compression"]}, m["class"])
    assert entry["size_basis"] == "source_unverified"
    assert "size_unverified" in entry["flags"]
    assert entry["vt_savings_mb"] is None
    assert entry["vt_supported"] is True and "enable_vt" in entry["actions"]
    assert entry["clamp_to"] is None


def test_h_dri_shape_keeps_clamp_action_but_no_money():
    """真实工程 hDRI 的形状（4096 立方 TC_HDR 冷态被推成 2365）：动作留、金额撤、不是 0.0。"""
    e = st.texture_pool_entry({"est_runtime_bytes": 2365 * 2365 * 4 * 6,
                               "source_width": 2365, "source_side_exact": False,
                               "max_size": 4096,
                               "compression": "TextureCompressionSettings.TC_HDR"}, "TextureCube")
    assert e["size_basis"] == "source_unverified"
    assert e["clamp_to"] == 2048 and "clamp_max_size" in e["actions"]
    assert e["clamp_savings_mb"] is None

HDR_ORDER = (8, 16, 4, 2, 1, 32, 64)          # 直读 compression_settings 后的候选顺序


def test_bpp_prior_by_compression_fixes_float_and_multiface():
    """工具面事实：bpp_candidates 覆盖顺序能定住浮点源边长（方案 1 作为产品已否决，见文末反证）。

    计数口径（工具面复算，非建议）：现状 2/6 -> 先验 3/6 -> 先验+面数 4/6；单通道 + TC_DEFAULT 仍歧义
    （它们的总量与 RGBA8 的半尺寸完全同解，无头信息可分）。
    """
    assert st.source_side_from_memory(MEASURED["T_CalibF32"]["src_mem"], 1,
                                      bpp_candidates=HDR_ORDER)[:3] == (512, 16, 1)
    assert st.source_side_from_memory(MEASURED["T_CalibCubeF32"]["src_mem"], 6,
                                      bpp_candidates=HDR_ORDER)[:3] == (256, 16, 6)
    assert st.source_side_from_memory(MEASURED["T_CalibF16"]["src_mem"], 1,
                                      bpp_candidates=HDR_ORDER)[:3] == (512, 8, 1)
    assert st.source_side_from_memory(MEASURED["T_CalibRGBA8"]["src_mem"], 1)[:3] == (1024, 4, 1)
    still_ambiguous = [n for n in ("T_CalibR8", "T_CalibR16")
                       if st.source_side_from_memory(MEASURED[n]["src_mem"], 1)[0] != AUTHORED[n][0]]
    assert sorted(still_ambiguous) == ["T_CalibR16", "T_CalibR8"]


def test_prior_would_move_the_money_on_the_f32_case():
    """同一张 512 浮点源：现状报 1024 边长 -> 按边长折算的金额与 VT 判定都会偏。

    面数/边长修正前后差 4 倍（1024^2 vs 512^2），这正是 #11 要收敛的量。
    """
    bad = st.source_side_from_memory(MEASURED["T_CalibF32"]["src_mem"], 1)
    good = st.source_side_from_memory(MEASURED["T_CalibF32"]["src_mem"], 1,
                                      bpp_candidates=HDR_ORDER)
    assert bad[0] == 1024 and good[0] == 512
    assert (bad[0] ** 2) / float(good[0] ** 2) == 4.0

# ---- 方案 1（compression 先验）反证：钉死"先验作为产品改动净亏"的两组实测数字 ----

# 夹具 T_CalibHdr 的源真值（5.3/5.4 与 59 项夹具同源）：引擎 TextureCube 副本，
# 源 6x128^2x4B；标 TC_HDR 但源是 8-bit——真实工程"标 HDR 的立方图"常见形态。
HDR_CUBE_SRC_MEM = 393216


def test_override_prior_flips_correct_answers_to_wrong():
    """覆盖式先验把"现状正确"的靶推错——方案 1 否决的第一根钉子。

    同一总量存在多个 2 的幂解时 source_memory 分不出真值，谁先试中谁成为
    exact=True：4194304 = 1024^2x4 = 512^2x16；393216 = 6x128^2x4 = 6x64^2x16。
    默认顺序先 4 恰好选对，HDR 先验先 8 再 16 就选错；且 compression 驱动的
    设计必动 TC_HDR 家族，T_CalibHdr 首当其冲——修好 F32 一例、推错两例。
    """
    assert st.source_side_from_memory(4194304, 1)[:2] == (1024, 4)
    assert st.source_side_from_memory(4194304, 1, HDR_ORDER)[:2] == (512, 16)
    assert st.source_side_from_memory(HDR_CUBE_SRC_MEM, 6)[:3] == (128, 4, 6)
    assert st.source_side_from_memory(HDR_CUBE_SRC_MEM, 6, HDR_ORDER) == (64, 16, 6, True)
    moved = sorted(n for n, m in MEASURED.items()
                   if st.source_side_from_memory(m["src_mem"], m["faces_used"],
                                                 HDR_ORDER)[:2] !=
                      st.source_side_from_memory(m["src_mem"], m["faces_used"])[:2])
    assert moved == ["T_CalibCubeF32", "T_CalibF32", "T_CalibR8", "T_CalibRGBA8"]


def test_fallback_only_prior_has_zero_gain_and_faces_fix_alone_backfires():
    """fallback 式零增益、faces 单修反噬——方案 1 否决的第二根钉子。

    实测面上默认顺序无解的只有 CubeF32（faces=1，slice 数在 5.3/5.4 不可直读：
    类级成员枚举无 array/slice/dim，探针已固化该负结论），而 faces=1 的先验
    照样无解 -> "默认失败才用先验"收益为 0。若只把 faces 修成 6 不动顺序，
    CubeF32 会从诚实 unverified 翻成自信的 (512,4) 伪精确并照常报价。
    面数与金额口径必须同批改，而两条路都堵死，故方案 1 整体否决。
    """
    no_exact = sorted(n for n, m in MEASURED.items()
                      if not st.source_side_from_memory(m["src_mem"],
                                                        m["faces_used"])[3])
    assert no_exact == ["T_CalibCubeF32"]
    assert st.source_side_from_memory(HDR_CUBE_SRC_MEM, 6)[3] is True
    m = MEASURED["T_CalibCubeF32"]
    assert st.source_side_from_memory(m["src_mem"], 1, HDR_ORDER) == (887, 8, 1, False)
    assert st.source_side_from_memory(m["src_mem"], 6) == (512, 4, 6, True)
