"""P1-1 真大/假大判定纯逻辑测试（无引擎，domain._runtime_verdict）。"""
from __future__ import annotations

from prism.domain import metrics as M

MB = 1048576.0


def test_disk_only_bloat_real_case():
    # 真机复盘案例：T_Dirt 磁盘 225MB 但运行时 8KB（MaxSize 限死）
    v = M._runtime_verdict(int(225 * MB), {"width": 4096, "height": 4096,
                                            "memory_bytes": 8 * 1024, "max_size": 128})
    assert v["verdict"] == "disk_only_bloat"
    assert v["capped_by_max_size"] is True
    assert v["runtime_mb"] < 1 and v["disk_mb"] > 200


def test_runtime_heavy_texture():
    v = M._runtime_verdict(int(30 * MB), {"width": 4096, "height": 4096,
                                          "memory_bytes": int(64 * MB)})
    assert v["verdict"] == "runtime_heavy"
    assert v["runtime_mb"] == 64.0


def test_normal_texture_runtime_ok():
    v = M._runtime_verdict(int(30 * MB), {"memory_bytes": int(4 * MB)})
    assert v["verdict"] == "normal"


def test_small_disk_not_bloat_candidate():
    # 源盘 < 20MB 不配谈"假大"，保持原判不动
    v = M._runtime_verdict(int(5 * MB), {"memory_bytes": int(1 * MB)})
    assert v["verdict"] == "normal"


def test_zero_mem_but_derived_estimate_bloat():
    # 常驻 0（未上传）+ 属性估算 20KB + 源盘 100MB -> 假大
    v = M._runtime_verdict(int(100 * MB), {"memory_bytes": 0, "est_runtime_bytes": 20 * 1024})
    assert v["verdict"] == "disk_only_bloat"
    assert v["runtime_basis"] == "derived_estimate"


def test_mesh_no_mem_uses_triangles():
    heavy = M._runtime_verdict(None, {"lods": [{"index": 0, "triangles": 350000}],
                                      "lod_count": 1})
    assert heavy["verdict"] == "runtime_heavy"
    assert heavy["triangles_top_lod"] == 350000
    ok = M._runtime_verdict(None, {"lods": [{"index": 0, "triangles": 1200}]})
    assert ok["verdict"] == "normal"


def test_missing_metrics_is_unknown():
    assert M._runtime_verdict(None, None)["verdict"] == "unknown"
    assert M._runtime_verdict(None, {})["verdict"] == "unknown"
    assert M._runtime_verdict(int(99 * MB), {"width": 1024})["verdict"] == "unknown"


def test_garbage_values_never_crash():
    v = M._runtime_verdict("not-a-number", {"memory_bytes": "NaN-ish"})
    assert v["verdict"] == "unknown"

def test_estimate_wins_over_unloaded_placeholder_memory():
    # 真机(5.4)校准场景：T_BigNoise 首查常驻仅 4KB(未上传)，属性估算 ~22MB -> 真大
    side = 4096
    est = int(side * side * 1.33)
    m = {"memory_bytes": 4096, "est_runtime_bytes": est}
    v = M._runtime_verdict(int(54 * MB), m)
    assert v["verdict"] == "runtime_heavy"
    assert v["runtime_basis"] == "derived_estimate"
    assert v["resident_mb"] == round(4096 / 1048576.0, 3)


class _FakeClass(object):
    def __init__(self, name):
        self._n = name

    def get_name(self):
        return self._n


class _FakeTex(object):
    def __init__(self, name):
        self._n = name

    def get_class(self):
        return _FakeClass(self._n)


def test_derive_dims_texture2d_and_cube():
    m = {"source_memory_bytes": 4096 * 4096 * 4, "width": 32, "height": 32}
    M._derive_texture_geometry(_FakeTex("Texture2D"), m)
    assert m["width"] == 4096 and m["size_derived"] is True
    assert m["est_runtime_bytes"] == int(4096 * 4096 * 1.33)
    # Cube: source 内存 = 6 面 RGBA8；128x128 立方图
    mc = {"source_memory_bytes": 128 * 128 * 6 * 4}
    M._derive_texture_geometry(_FakeTex("TextureCube"), mc)
    assert mc["width"] == 128
    assert mc["est_runtime_bytes"] == int(128 * 128 * 6 * 1.33)


def test_derive_respects_max_size_cap():
    m = {"source_memory_bytes": 4096 * 4096 * 4, "max_size": 128}
    M._derive_texture_geometry(_FakeTex("Texture2D"), m)
    assert m["width"] == 4096  # 边长仍是源尺寸
    assert m["est_runtime_bytes"] == int(128 * 128 * 1.33)  # 估算按限幅后

def test_warm_state_keeps_effective_and_source_widths():
    # 热态直读=MaxSize 限幅后的有效尺寸 128；源反推 4096 记入 source_width（规则判据）
    m = {"width": 128, "height": 128, "max_size": 128,
         "source_memory_bytes": 4096 * 4096 * 4}
    M._derive_texture_geometry(_FakeTex("Texture2D"), m)
    assert m["source_width"] == 4096
    assert m["effective_width"] == 128
    assert m["width"] == 128 and "size_derived" not in m
    assert m["est_runtime_bytes"] == int(128 * 128 * 1.33)


def test_material_slots_from_lods_pure():
    assert M._material_slots_from_lods([{"index": 0, "sections": 7}]) == 7
    assert M._material_slots_from_lods([{"index": 0, "sections": 3},
                                        {"index": 1, "sections": 5}]) == 5   # 跨 LOD 取最大
    assert M._material_slots_from_lods([{"index": 0}]) is None                # 无 sections
    assert M._material_slots_from_lods([]) is None
    assert M._material_slots_from_lods(None) is None


def test_derive_cube_hdr_uses_8bpp_not_rgba8():
    """5.4 真机回归：T_HDR 源内存 201326592 = 6 x 2048^2 x 8B(HDR/RGBA16)。
    一律按 4B/px 反推会得出 2896 的假尺寸，进而编出"钳到 2048 省 96MB"的假收益。"""
    mc = {"source_memory_bytes": 2048 * 2048 * 6 * 8}
    M._derive_texture_geometry(_FakeTex("TextureCube"), mc)
    assert mc["source_width"] == 2048
    assert mc["source_bytes_per_texel"] == 8
    assert mc["source_side_exact"] is True
    assert mc["est_runtime_bytes"] == int(2048 * 2048 * 6 * 1.33)


def test_derive_cube_panorama_reports_face_side_4096():
    """#11 D 落到度量层：2:1 全景立方的 source_width 必须是面边长 4096（旧口径 2365），
    并回传命中的存法 source_layout；运行时估算按 6 面，不再低估 3 倍。"""
    mc = {"source_memory_bytes": 2 * 4096 * 4096 * 4}
    M._derive_texture_geometry(_FakeTex("TextureCube"), mc)
    assert mc["source_width"] == 4096
    assert mc["source_side_exact"] is True
    assert mc["source_layout"] == "panorama_2to1"
    assert mc["est_runtime_bytes"] == int(4096 * 4096 * 6 * 1.33)


def test_derive_slice_array_is_not_treated_as_cube():
    """6 个 slice 的 2D Array 面数同为 6，但不走双假设：不标 layout、答案与旧口径一致。"""
    mc = {"source_memory_bytes": 6 * 2048 * 2048 * 4, "array_size": 6}
    M._derive_texture_geometry(_FakeTex("Texture2DArray"), mc)
    assert mc["source_width"] == 2048 and mc["source_side_exact"] is True
    assert "source_layout" not in mc


def test_derive_marks_inexact_side_when_not_pow2():
    m = {"source_memory_bytes": 3000 * 3000 * 4}
    M._derive_texture_geometry(_FakeTex("Texture2D"), m)
    assert m["source_width"] == 3000 and m["source_side_exact"] is False
    assert m["source_bytes_per_texel"] == 4


def test_verdict_high_bpp_measured_is_not_multiplied_again():
    """真机 T_HDR：实测 201326592B 已含 8B/px 格式。判定与池估算同源走
    runtime_ledger，采信实测不得再乘倍率（旧 x4 兜底会把 192MB 报成 768MB）。"""
    v = M._runtime_verdict(int(288 * MB), {"memory_bytes": 201326592,
                                           "est_runtime_bytes": 33470545,
                                           "compression": "TextureCompressionSettings.TC_HDR"})
    assert v["verdict"] == "runtime_heavy"
    assert v["runtime_mb"] == 192.0
    assert v["runtime_basis"] == "resident_memory"
    assert v["bytes_per_texel"] == 8.0
    assert "resident_mb" not in v                 # 采信值就是实测值，不重复记一遍


def test_verdict_high_bpp_cold_upload_scales_estimate():
    """冷态实测只是占位 -> 按 BC 近似 x8 兜底，别再用 1B/px 低估成 normal。"""
    v = M._runtime_verdict(int(30 * MB), {"memory_bytes": 24576,
                                          "est_runtime_bytes": int(4 * MB),
                                          "compression": "TextureCompressionSettings.TC_HDR"})
    assert v["verdict"] == "runtime_heavy"
    assert v["runtime_mb"] == 32.0
    assert v["runtime_basis"] == "derived_estimate"
    assert v["resident_mb"] == round(24576 / 1048576.0, 3)
