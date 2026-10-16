"""LOD 族离线用例：判据分类、跨版本 API 形态兜底、无引擎降级结构。

探针已把可达面钉死（docs/TESTING.md「LOD 族立项前取证」）：每级 ScreenSize 读写都不通，
每平台 Min LOD 可读写。所以这里的判据断言就是那条产品承诺的边界——
不给「阈值不合理」结论、收益只在能实测到建议级三角数时才给数字（宁缺勿假）。
"""
from __future__ import annotations

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