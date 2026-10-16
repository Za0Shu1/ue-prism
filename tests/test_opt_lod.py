"""LOD 族离线用例：判据分类、跨版本 API 形态兜底、无引擎降级结构。

探针已把可达面钉死（docs/TESTING.md「LOD 族立项前取证」）：每级 ScreenSize 读写都不通，
每平台 Min LOD 可读写。所以这里的判据断言就是那条产品承诺的边界——
不给「阈值不合理」结论、收益只在能实测到建议级三角数时才给数字（宁缺勿假）。
"""
from __future__ import annotations

import json
import os

from prism import envelope
from prism.domain import FUNCTIONS, opt_lod as ol


def _lods(*tris):
    return [{"index": i, "triangles": t} for i, t in enumerate(tris)]


# ---------- judge：分类 ----------

def test_judge_nanite_managed_wins_and_gives_no_reduction():
    cat, flags, ratio, target, pot = ol.judge(4, _lods(500000, 200000, 50000, 10000),
                                              True, False, {}, 200000)
    assert cat == "nanite_managed"
    assert pot == 0 and target is None and ratio == 0.4
    assert "min_lod_unset" in flags          # 事实陈述照给，但不因此报候选


def test_judge_no_lod_highpoly_is_action_only_without_numbers():
    cat, flags, ratio, target, pot = ol.judge(1, _lods(900000), False, True, {}, 200000)
    assert cat == "no_lod_highpoly"
    assert pot == 0 and target is None
    assert "screen_size_auto_computed" in flags


def test_judge_single_lod_lowpoly_is_ok():
    cat, _f, _r, _t, pot = ol.judge(1, _lods(48), False, True, {}, 200000)
    assert cat == "ok" and pot == 0


def test_judge_weak_chain_when_first_cut_did_not_reduce():
    cat, _f, ratio, target, pot = ol.judge(3, _lods(400000, 380000, 20000),
                                           False, False, {}, 200000)
    assert cat == "weak_lod_chain"
    assert ratio == 0.95 and target == 2 and pot == 380000


def test_judge_min_lod_actionable_when_chain_is_healthy_and_uncovered():
    cat, flags, ratio, target, pot = ol.judge(3, _lods(400000, 60000, 20000),
                                              False, False, {"ALL": -1}, 200000)
    assert cat == "min_lod_actionable"
    assert ratio == 0.15 and target == 2 and pot == 380000
    assert "min_lod_unset" in flags


def test_judge_ok_when_platform_override_already_exists():
    # 阶梯健康 + 已有 Min LOD 覆盖：不重复报候选（min_lod=0 也算覆盖）
    for override in ({"ALL": 1}, {"Windows": 0}):
        cat, flags, _r, _t, pot = ol.judge(3, _lods(400000, 60000, 20000),
                                           False, False, override, 200000)
        assert cat == "ok", override
        assert pot == 0 and "min_lod_unset" not in flags


def test_judge_ok_below_triangle_threshold():
    cat, _f, _r, _t, pot = ol.judge(3, _lods(150000, 10000, 2000), False, False, {}, 200000)
    assert cat == "ok" and pot == 0


def test_judge_unreadable():
    assert ol.judge(None, [], False, False, {}, 200000)[0] == "unreadable"
    assert ol.judge(1, [{"index": 0}], False, False, {}, 200000)[0] == "unreadable"


def test_judge_weak_chain_still_actionable_even_when_covered():
    # 阶梯本身没降下来：覆盖过不等于够用，动作面（提高 Min LOD）依然成立
    cat, _f, _r, target, pot = ol.judge(3, _lods(400000, 390000, 20000),
                                        False, False, {"ALL": 1}, 200000)
    assert cat == "weak_lod_chain" and target == 2 and pot == 380000


def test_judge_reduction_unmeasured_instead_of_borrowing_a_number():
    # metrics 窗口只到 LOD7：建议级 9 没实测值 -> 报候选但不给收益数字
    lods = _lods(400000, 100000, 90000, 80000, 70000, 60000, 50000, 40000)
    cat, flags, _r, target, pot = ol.judge(10, lods, False, False, {}, 200000)
    assert cat == "min_lod_actionable" and target == 9 and pot == 0
    assert "reduction_unmeasured" in flags


def test_judge_flags_for_unknown_apis():
    cat, flags, _r, _t, _p = ol.judge(3, _lods(400000, 60000, 20000), None, None, {}, 200000)
    assert "nanite_unknown" in flags and "auto_compute_unknown" in flags
    assert "screen_size_auto_computed" not in flags


def test_judge_missing_triangles_do_not_produce_ratio_or_potential():
    lods = [{"index": 0}, {"index": 1}]
    cat, flags, ratio, target, pot = ol.judge(2, lods, False, False, {}, 200000)
    assert cat == "unreadable" and ratio is None and pot == 0 and target is None


def test_judge_infers_num_lods_from_rows_when_api_silent():
    cat, _f, ratio, target, pot = ol.judge(None, _lods(400000, 50000), False, False, {}, 200000)
    assert cat == "min_lod_actionable" and target == 1 and pot == 350000


# ---------- _try_call：多形态兜底 ----------

class _Obj(object):
    def __init__(self, results):
        self._results = dict(results)
        self._calls = []

    def __getattr__(self, name):
        if name in self._results:
            spec = self._results[name]
            def _fn(*args, _n=name, _s=spec):
                self._calls.append(((_n, ) + args, ))
                if isinstance(_s, Exception):
                    raise _s
                return _s
            return _fn
        raise AttributeError(name)


def test_try_call_returns_first_shape_and_via_label():
    o = _Obj({"get_num_lods": 3})
    v, via, tried = ol._try_call(o, "get_num_lods", [((), "noarg"), ((0,), "int")])
    assert v == 3 and via == "get_num_lods:noarg" and tried == []


def test_try_call_falls_through_shapes_and_keeps_tried():
    class _Second(object):
        def __init__(self):
            self.n = 0

        def get_minimum_lod_for_platform(self, key):
            self.n += 1
            if self.n == 1:
                raise TypeError("bad arg shape")
            return 2

    v, via, tried = ol._try_call(_Second(), "get_minimum_lod_for_platform",
                                 [(("ALL", ), "ALL"), (("Windows", ), "Windows")])
    assert v == 2 and via.endswith(":Windows")
    assert tried[0]["err"] == "TypeError"


def test_try_call_absent_method_is_honest():
    v, via, tried = ol._try_call(object(), "no_such_lod_api", [((), "noarg")])
    assert v is None and via is None
    assert tried == [{"sig": "no_such_lod_api", "err": "absent"}]


# ---------- _map_to_plain：UE Map 面 ----------

class _FakeMap(object):
    def __init__(self, d):
        self._d = dict(d)

    def keys(self):
        return list(self._d)

    def __getitem__(self, k):
        return self._d[k]


class _ToDictOnly(object):
    def to_dict(self):
        return {"ALL": 1, "Android": -1}


def test_map_to_plain_accepts_dict_and_map_like():
    assert ol._map_to_plain({"ALL": 2}) == {"ALL": 2}
    assert ol._map_to_plain(_FakeMap({"ALL": 1, "Windows": None})) == {"ALL": 1, "Windows": -1}
    assert ol._map_to_plain(_ToDictOnly()) == {"ALL": 1, "Android": -1}


def test_map_to_plain_none_and_garbage():
    assert ol._map_to_plain(None) is None
    assert ol._map_to_plain(object()) is None
    assert ol._map_to_plain({"": 5, "ALL": 1}) == {"ALL": 1}


# ---------- read_auto_compute / read_min_lod ----------

def test_read_auto_compute_bool_and_unknown():
    v, via, tried = ol.read_auto_compute(_Obj({"is_lod_screen_size_auto_computed": True}))
    assert v is True and via == "is_lod_screen_size_auto_computed:noarg"
    v2, via2, tried2 = ol.read_auto_compute(object())
    assert v2 is None and via2 is None and tried2[0]["err"] == "absent"


def test_read_min_lod_merges_map_and_per_platform():
    class _Both(object):
        def get_minimum_lod_for_platforms(self):
            return _FakeMap({"Android": 2})

        def get_minimum_lod_for_platform(self, key):
            return 3 if key == "ALL" else -1

    out, via, tried = ol.read_min_lod(_Both())
    assert out["Android"] == 2 and out["ALL"] == 3
    assert via == "get_minimum_lod_for_platforms:noarg"


def test_read_min_lod_without_map_api_falls_back_to_per_platform():
    class _PerPlatform(object):
        def get_minimum_lod_for_platform(self, key):
            return -1

    out, via, _tried = ol.read_min_lod(_PerPlatform())
    assert via == "get_minimum_lod_for_platform:ALL"   # 代表签名取首个命中
    assert set(out) == set(ol.PLATFORM_KEYS)
    assert all(v == -1 for v in out.values())


def test_read_min_lod_all_absent_returns_empty():
    out, via, tried = ol.read_min_lod(object())
    assert out == {} and via is None and tried


# ---------- 无引擎降级 & 参数回报 ----------

def test_scan_degrades_with_structure_keys():
    r = ol.scan_mesh_lod()
    assert r["note"] == "no_engine"
    assert {"scope", "thresholds", "summary", "items", "candidates",
            "total", "truncated", "cap", "api", "rejected"} <= set(r)
    assert set(r["candidates"]) == {"no_lod_highpoly", "weak_lod_chain", "force_min_lod"}
    assert r["items"] == [] and r["total"] == 0 and r["truncated"] is False
    for cat in ol.CATEGORIES:
        assert r["summary"][cat] == 0


def test_scan_reports_thresholds_and_cap():
    r = ol.scan_mesh_lod(min_triangles=123456, top=5)
    assert r["thresholds"]["min_triangles"] == 123456 and r["cap"] == 5
    assert r["thresholds"]["weak_ratio"] == ol.WEAK_RATIO


def test_scan_is_registered():
    assert "scan_mesh_lod" in FUNCTIONS
    assert FUNCTIONS["scan_mesh_lod"] is ol.scan_mesh_lod

# ---------- 1b：opt_mesh_set_min_lod（写·双钥）----------

class _PMap(object):
    def __init__(self, d):
        self._d = dict(d)

    def keys(self):
        return list(self._d)

    def __getitem__(self, k):
        return self._d[k]


class _MinLodMesh(object):
    """StaticMesh 替身：LOD 面 + 每平台 Min LOD 读写面 + Nanite 平坦属性。

    silent=True 模拟「写调用没报错但值没进去」——这是最容易被假装成功的一类，
    工具必须靠回读校验把它判成 failed。
    """

    def __init__(self, tris, min_lod=None, nanite=None, platform_map=None,
                 no_setter=False, silent=False):
        object.__setattr__(self, "_tris", list(tris))
        object.__setattr__(self, "_minlod", dict(min_lod or {}))
        object.__setattr__(self, "_silent", silent)
        if nanite is not None:
            object.__setattr__(self, "nanite_enabled", nanite)
        if platform_map is not None:
            object.__setattr__(self, "get_minimum_lod_for_platforms",
                               lambda: _PMap(platform_map))
        if not no_setter:
            def _set(key, value, _s=self):
                if not _s._silent:
                    _s._minlod[str(key)] = int(value)
            object.__setattr__(self, "set_minimum_lod_for_platform", _set)

    def get_num_lods(self):
        return len(self._tris)

    def get_num_triangles(self, lod):
        return self._tris[lod]

    def get_num_sections(self, lod):
        return 1

    def get_minimum_lod_for_platform(self, key):
        return int(self._minlod.get(str(key), -1))

    def is_lod_screen_size_auto_computed(self):
        return True

    def get_editor_property(self, key):
        raise AttributeError(key)


def _tried_has(tried, needle):
    return any(needle in str(row) for row in tried)


def test_to_int_distinguishes_missing_from_zero():
    assert ol._to_int(None, None) is None
    assert ol._to_int("", None) is None
    assert ol._to_int("3", None) == 3
    assert ol._to_int(3.0, None) == 3
    assert ol._to_int("-1", None) == -1
    assert ol._to_int(0, None) == 0            # 0 是合法级号，不能当"没给"
    assert ol._to_int(True, None) is None      # bool 不是级号
    assert ol._to_int("abc", -7) == -7


def test_write_min_lod_roundtrip_verifies_by_readback():
    mesh = _MinLodMesh([100000, 20000], min_lod={"ALL": -1})
    ok, api, _tried = ol._write_min_lod(mesh, "ALL", 1)
    assert ok is True and api.startswith("set_minimum_lod_for_platform(ALL,")
    assert mesh.get_minimum_lod_for_platform("ALL") == 1


def test_write_min_lod_silent_noop_is_failed_not_success():
    mesh = _MinLodMesh([100000, 20000], min_lod={"ALL": -1}, silent=True)
    ok, api, tried = ol._write_min_lod(mesh, "ALL", 1)
    assert ok is False and api is not None
    assert _tried_has(tried, "mismatch")


def test_write_min_lod_absent_setter_reports_no_api():
    mesh = _MinLodMesh([100000, 20000], no_setter=True)
    ok, api, tried = ol._write_min_lod(mesh, "ALL", 1)
    assert ok is False and api is None and tried


def test_write_min_lod_already_at_target_writes_nothing():
    mesh = _MinLodMesh([100000, 20000, 1000], min_lod={"ALL": 1})
    ok, api, _tried = ol._write_min_lod(mesh, "ALL", 1)
    assert ok is True and api == "no_write_needed"


def test_write_target_missing_after_is_failed():
    mesh = _MinLodMesh([100000, 20000])
    ok, api, tried = ol.write_target(mesh, {"platform": "ALL"}, "ALL")
    assert ok is False and api is None and tried[0]["err"] == "missing"


def test_write_target_uses_row_platform_when_given():
    mesh = _MinLodMesh([400000, 60000, 20000], min_lod={"Windows": -1})
    ok, api, _tried = ol.write_target(mesh, {"platform": "Windows", "after": 1}, "ALL")
    assert ok is True and api.startswith("set_minimum_lod_for_platform(Windows,")
    assert mesh.get_minimum_lod_for_platform("Windows") == 1


def test_inspect_auto_picks_deepest_lod_and_measures_reduction():
    tried = []
    row = ol.inspect_target(None, _MinLodMesh([400000, 60000, 20000]),
                            {"package": "/Game/M/X"}, "ALL", None, True, 200000, tried)
    assert row["kind"] == "change" and row["after"] == 2 and row["suggested_min_lod"] == 2
    assert row["potential_triangles"] == 380000 and row["before"] == -1
    assert row["platform"] == "ALL" and row["num_lods"] == 3


def test_inspect_explicit_min_lod_is_honoured():
    row = ol.inspect_target(None, _MinLodMesh([400000, 60000, 20000]), {},
                            "ALL", 1, True, 200000, [])
    assert row["kind"] == "change" and row["after"] == 1
    assert row["potential_triangles"] == 340000


def test_inspect_min_lod_beyond_lod_count_is_refused():
    row = ol.inspect_target(None, _MinLodMesh([400000, 60000]), {},
                            "ALL", 5, True, 200000, [])
    assert row["kind"] == "skip" and row["after"] == row["before"]
    assert "拒写" in row["reason"]


def test_inspect_single_lod_mesh_has_nothing_to_reduce():
    mesh = _MinLodMesh([900000])
    row = ol.inspect_target(None, mesh, {}, "ALL", None, True, 200000, [])
    assert row["kind"] == "skip" and "一级 LOD" in row["reason"]
    # 显式清覆盖（-1）仍允许：那是把平台设置还原给引擎，不是降级
    row2 = ol.inspect_target(None, mesh, {}, "ALL", -1, True, 200000, [])
    assert row2["kind"] == "no_op" and row2["after"] == -1


def test_inspect_nanite_gate_blocks_by_default():
    row = ol.inspect_target(None, _MinLodMesh([400000, 60000], nanite=True), {},
                            "ALL", None, True, 200000, [])
    assert row["kind"] == "skip" and "Nanite" in row["reason"]
    forced = ol.inspect_target(None, _MinLodMesh([400000, 60000], nanite=True), {},
                               "ALL", None, False, 200000, [])
    assert forced["kind"] == "change"


def test_inspect_low_poly_gate_uses_scan_threshold():
    mesh = _MinLodMesh([120000, 3000])
    row = ol.inspect_target(None, mesh, {}, "ALL", None, True, 200000, [])
    assert row["kind"] == "skip" and "阈值" in row["reason"]
    assert ol.inspect_target(None, mesh, {}, "ALL", None, True, 0, [])["kind"] == "change"


def test_inspect_no_op_when_platform_already_set():
    row = ol.inspect_target(None, _MinLodMesh([400000, 60000, 20000], min_lod={"ALL": 2}),
                            {}, "ALL", None, True, 200000, [])
    assert row["kind"] == "no_op" and row["after"] == 2


def test_inspect_unreadable_lod_count_is_unknown_not_change():
    class _Blind(_MinLodMesh):
        def get_num_lods(self):
            raise AttributeError("no such binding")

        def get_num_triangles(self, lod):
            raise AttributeError("no such binding")

    row = ol.inspect_target(None, _Blind([0, 0]), {}, "ALL", None, True, 200000, [])
    assert row["kind"] == "unknown" and row["num_lods"] == 0


def test_inspect_reduction_unmeasured_when_target_outside_window():
    mesh = _MinLodMesh([400000] + [9000] * 8)      # 9 级：建议级 8 落在 LOD0-7 窗口外
    row = ol.inspect_target(None, mesh, {}, "ALL", None, True, 200000, [])
    assert row["kind"] == "change" and row["after"] == 8
    assert row["potential_triangles"] == 0
    assert row["detail"]["note"] == "reduction_unmeasured"


def test_inspect_restore_mode_bypasses_gates():
    row = ol.inspect_target(None, _MinLodMesh([900000], min_lod={"ALL": 1}), {},
                            "ALL", None, True, 200000, [], restore_value=-1)
    assert row["kind"] == "change" and row["after"] == -1
    assert row["detail"]["mode"] == "restore"
    same = ol.inspect_target(None, _MinLodMesh([900000], min_lod={"ALL": 1}), {},
                             "ALL", None, True, 200000, [], restore_value=1)
    assert same["kind"] == "no_op"


def test_lod_rows_reports_window_and_per_lod_tris():
    tried = []
    num, per, via = ol._lod_rows(None, _MinLodMesh([400000, 60000, 20000]), tried)
    assert num == 3 and per[0] == 400000 and per[2] == 20000
    assert via.startswith("get_num_lods")


def test_manifest_roundtrip_is_atomic(tmp_path):
    path = str(tmp_path / "sub" / "minlod.json")
    payload = {"version": ol.MANIFEST_VERSION, "op": ol.OP,
               "entries": [{"package": "/Game/M/X", "platform": "ALL",
                            "before": -1, "after": 2, "saved": True}]}
    written = ol._write_json_atomic(path, payload)
    assert written == path and not os.path.exists(path + ".tmp")
    assert ol._read_manifest(path) == payload


def test_manifest_rejects_garbage(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert ol._read_manifest(str(bad)) is None
    no_entries = tmp_path / "noe.json"
    no_entries.write_text(json.dumps({"op": ol.OP}), encoding="utf-8")
    assert ol._read_manifest(str(no_entries)) is None


def test_manifest_path_prefers_given_then_saved(tmp_path):
    given = str(tmp_path / "mine.json")
    assert ol._manifest_path(None, given, ol.OP) == given
    auto = ol._manifest_path(None, None, ol.OP)
    assert auto.endswith(".json") and ol.OP in os.path.basename(auto)
    assert "Prism" in auto


def test_write_op_requires_double_key():
    env = ol.opt_mesh_set_min_lod(paths="/Game/M/X", dry_run=False, confirm=False)
    assert env["ok"] is False and "双钥" in env["error"]["message"]


def test_write_op_degrades_honestly():
    env = ol.opt_mesh_set_min_lod(paths="/Game/M/X")
    assert env["ok"] is True
    r = env["result"]
    assert r["note"] == "no_engine" and r["executed"] is False and r["success"] is False
    assert r["mode"] == "apply" and r["platform"] == "ALL"
    assert r["rollback_available"] is False and r["manifest_path"] is None
    assert r["dry_run"] is True and r["targets"] == []
    assert set(r) >= {"thresholds", "restore_manifest", "changed", "skipped", "failed"}


def test_restore_requires_readable_manifest(tmp_path):
    missing = str(tmp_path / "nope.json")
    env = ol.opt_mesh_set_min_lod(restore=missing)
    assert env["ok"] is False and env["error"]["code"] == envelope.Code.RUNTIME_ERROR
    assert "清单" in env["error"]["message"]

    other = tmp_path / "other.json"
    other.write_text(json.dumps({"version": 1, "op": "opt_collapse_hism",
                                 "entries": [{"package": "/Game/M/X"}]}), encoding="utf-8")
    env2 = ol.opt_mesh_set_min_lod(restore=str(other))
    assert env2["ok"] is False and "不是本工具写的" in env2["error"]["message"]

    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps({"version": 1, "op": ol.OP, "entries": []}), encoding="utf-8")
    env3 = ol.opt_mesh_set_min_lod(restore=str(empty))
    assert env3["ok"] is False and "没有条目" in env3["error"]["message"]


def test_write_op_is_registered():
    assert "opt_mesh_set_min_lod" in FUNCTIONS