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
