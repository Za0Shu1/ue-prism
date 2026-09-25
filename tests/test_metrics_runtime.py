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


def test_zero_mem_defends_division():
    v = M._runtime_verdict(int(100 * MB), {"memory_bytes": 0})
    assert v["verdict"] == "disk_only_bloat"


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
