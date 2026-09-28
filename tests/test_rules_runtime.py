"""P1-1 报告联动测试：apply_runtime_verdicts 消 asset_size_top 误报（纯函数，无引擎）。"""
from __future__ import annotations

from prism import rules


def _finding(sev="error", subject="/Game/T/Big"):
    return {"rule_id": "asset_size_top", "severity": sev, "subject": subject,
            "evidence": {"size_mb": 225.0}, "threshold": 100.0,
            "advice": "check resolution/complexity"}


def _metrics(verdict, subject="/Game/T/Big", runtime_mb=0.008, disk_mb=225.0):
    return {subject: {"metrics": {"runtime_verdict": {
        "verdict": verdict, "runtime_mb": runtime_mb, "disk_mb": disk_mb}}}}


def test_fake_large_downgraded_error_to_warn():
    f = _finding()
    n = rules.apply_runtime_verdicts([f], _metrics("disk_only_bloat"))
    assert n == 1
    assert f["severity"] == "warn"
    assert f["evidence"]["runtime_verdict"] == "disk_only_bloat"
    assert f["evidence"]["runtime_mb"] == 0.008
    assert "source-disk bloat" in f["advice"]


def test_runtime_heavy_untouched():
    f = _finding()
    n = rules.apply_runtime_verdicts([f], _metrics("runtime_heavy", runtime_mb=64.0))
    assert n == 0 and f["severity"] == "error"


def test_unknown_or_missing_metrics_untouched():
    f = _finding()
    g = _finding(subject="/Game/T/Other")
    assert rules.apply_runtime_verdicts([f, g], _metrics("unknown")) == 0
    assert rules.apply_runtime_verdicts([f, g], {}) == 0
    assert f["severity"] == "error" and g["severity"] == "error"


def test_other_rules_ignored():
    f = {"rule_id": "texture_size", "severity": "error", "subject": "/Game/T/Big",
         "evidence": {}, "threshold": 4096, "advice": "x"}
    assert rules.apply_runtime_verdicts([f], _metrics("disk_only_bloat")) == 0
    assert f["severity"] == "error"


def test_warn_stays_warn_but_gets_evidence():
    f = _finding(sev="warn")
    rules.apply_runtime_verdicts([f], _metrics("disk_only_bloat"))
    assert f["severity"] == "warn"
    assert f["evidence"]["runtime_verdict"] == "disk_only_bloat"

def test_texture_size_capped_4k_downgraded_and_real_4k_stays():
    """5.4 真机校准固化：MaxSize 限幅的假大 4K 降 warn，无限幅真 4K 保持 error。"""
    ctx = {"profile": rules.PROFILES["pc"],
           "metrics": {
               "/Game/T/Fake": {"metrics": {"width": 4096, "height": 4096,
                                    "memory_bytes": 4096, "max_size": 128,
                                    "est_runtime_bytes": int(128 * 128 * 1.33)}},
               "/Game/T/Real": {"metrics": {"width": 4096, "height": 4096,
                                    "memory_bytes": 4096,
                                    "est_runtime_bytes": int(4096 * 4096 * 1.33)}},
           }}
    out = {f["subject"]: f for f in rules._rule_texture_size(ctx)}
    fake, real = out["/Game/T/Fake"], out["/Game/T/Real"]
    assert fake["severity"] == "warn" and real["severity"] == "error"
    assert fake["evidence"]["est_runtime_mb"] < 1
    assert fake["evidence"]["resident_bytes"] == 4096
    assert "capped by MaxSize" in fake["advice"]
    assert "est_runtime_mb" in real["evidence"]


# ---- D2: mesh 材质槽维度流进报告（5.4 校准：从 sections 派生 material_slots）----

def test_mesh_tri_evidence_carries_material_slots():
    ctx = {"profile": rules.PROFILES["pc"],
           "metrics": {"/Game/M/High": {"metrics": {
               "lods": [{"index": 0, "triangles": 2500000, "sections": 9}],
               "lod_count": 1, "material_slots": 9}}}}
    f = rules._rule_mesh_tri(ctx)
    assert len(f) == 1
    assert f[0]["evidence"]["material_slots"] == 9
    assert f[0]["evidence"]["triangles_lod0"] == 2500000
    assert "material slots" in f[0]["advice"]     # 9 >= mat_slots_warn(8) -> 追加建议


def test_mesh_tri_low_material_slots_no_advice():
    ctx = {"profile": rules.PROFILES["pc"],
           "metrics": {"/Game/M/Ok": {"metrics": {
               "lods": [{"index": 0, "triangles": 300000, "sections": 2}],
               "lod_count": 3, "material_slots": 2}}}}
    f = rules._rule_mesh_tri(ctx)
    assert len(f) == 1 and f[0]["evidence"]["material_slots"] == 2
    assert "material slots" not in f[0]["advice"]
