"""scan_missing_source_files 离线测试：无引擎降级结构键 + 导入记录多签名 + 纯逻辑聚合。

对应 AGENTS.md「测试随功能走」：新 domain 函数必须带无引擎降级用例（结构键常驻）。
`_import_record` 走的是纯 Python 鸭子类型假对象，把「Interchange / 旧管线 / 属性面 /
无记录」四条真机签名分支在无引擎下也钉住——真机只跑一次 L2 探针，这里锁住判据。
"""
from __future__ import annotations

import threading

from prism import bridge, bus
from prism.domain import assets

KEYS = {"folder", "class_contains", "found_registry", "total_available", "scanned",
        "offset", "cap", "truncated", "rows", "rows_truncated", "with_record",
        "no_record", "present_count", "partial_count", "missing_count",
        "load_failed_count", "missing_bytes", "missing_mb", "skipped",
        "skipped_count", "by_dir", "by_dir_truncated", "api", "note"}


def _bridge(bdir):
    threading.Thread(target=bridge.run_forever, args=(bdir, 0.02), daemon=True).start()


# ---------- 无引擎降级 ----------

def test_degrade_keeps_every_structural_key(tmp_path):
    bdir = str(tmp_path / "bus")
    _bridge(bdir)
    env = bus.BusClient(bdir, timeout=5).call("scan_missing_source_files", {"folder": "/Game"})
    assert env["ok"] is True
    r = env["result"]
    assert KEYS.issubset(set(r.keys())), sorted(KEYS - set(r.keys()))
    assert r["found_registry"] is False
    assert r["note"].startswith("no_engine")
    assert r["rows"] == [] and r["missing_count"] == 0 and r["missing_mb"] == 0.0
    assert r["by_dir"] == {} and r["api"] == {"import_record_via": {}}


def test_degrade_normalizes_folder_and_limits(tmp_path):
    bdir = str(tmp_path / "bus")
    _bridge(bdir)
    env = bus.BusClient(bdir, timeout=5).call(
        "scan_missing_source_files",
        {"folder": "Tex/Char", "class_contains": "TEXTURE", "limit": "12",
         "offset": "-3", "max_items": "0"})
    r = env["result"]
    assert r["folder"] == "/Game/Tex/Char"        # /Game 前缀自动补齐
    assert r["class_contains"] == "texture"       # 归一小写，空串才回 None
    assert r["cap"] == 12                         # 字符串数字照单归一
    assert r["offset"] == 0 and r["truncated"] is False
    assert KEYS.issubset(set(r.keys()))


def test_degrade_bad_numbers_fall_back_to_defaults(tmp_path):
    bdir = str(tmp_path / "bus")
    _bridge(bdir)
    env = bus.BusClient(bdir, timeout=5).call(
        "scan_missing_source_files", {"limit": "abc", "offset": None})
    r = env["result"]
    assert r["cap"] == 200 and r["offset"] == 0


# ---------- 导入记录通道（多签名兜底） ----------

class _SI(object):
    """假的 import-data 对象：只挂上这份「引擎构建」真的有的签名，其余 getattr 落空。

    真实跨版本差异就是签名有无（Interchange 只有 extract_*，旧管线只有 get_first_*），
    照实模拟才能验出兜底顺序，而不是让四个方法全都存在。
    """

    def __init__(self, methods=None, props=None):
        self._props = dict(props or {})
        for name, value in (methods or {}).items():
            setattr(self, name, value)

    def get_editor_property(self, name):
        if name in self._props:
            return self._props[name]
        raise KeyError(name)


class _Obj(object):
    def __init__(self, si=None, raise_on=()):
        self._si = si
        self._raise_on = set(raise_on)

    def get_editor_property(self, name):
        if name in self._raise_on:
            raise Exception("property not bound on this build")
        if name in ("asset_import_data", "import_data"):
            return self._si
        raise KeyError(name)


def test_import_record_interchange_first_signature():
    si = _SI(methods={"extract_filenames": lambda: ["D:/Src/A.png", "D:/Src/B.png"]})
    files, via, note = assets._import_record(_Obj(si))
    assert files == ["D:/Src/A.png", "D:/Src/B.png"] and via == "extract_filenames"
    assert note is None


def test_import_record_str_result_wrapped_and_skips_empty():
    """extract_filenames 回空 -> 继续下一个签名；单 str 结果要包成 list。"""
    si = _SI(methods={"extract_filenames": lambda: [],
                      "script_extract_filenames": lambda: [],
                      "get_first_filename": lambda: "D:/Src/T.tex"})
    files, via, _note = assets._import_record(_Obj(si))
    assert files == ["D:/Src/T.tex"] and via == "get_first_filename"


def test_import_record_falls_back_to_property_face():
    si = _SI(methods={"extract_filenames": lambda: (_ for _ in ()).throw(RuntimeError("boom")),
                      "script_extract_filenames": lambda: [],
                      "get_first_filename": lambda: [],
                      "script_get_first_filename": lambda: []},
             props={"filenames": ["D:/Src/M.fbx"]})
    files, via, note = assets._import_record(_Obj(si))
    assert files == ["D:/Src/M.fbx"] and via == "prop:filenames"
    assert note and "extract_filenames" in note       # 试过的错面如实带出，不静默


def test_import_record_no_data_and_empty_record_are_distinct():
    files, via, note = assets._import_record(_Obj(raise_on=("asset_import_data", "import_data")))
    assert files == [] and via is None and note == "no_import_data"
    files, via, note = assets._import_record(_Obj(_SI()))
    assert files == [] and via is None and note == "empty_record"


def test_import_record_never_raises_on_hostile_values():
    """脏值（None/空白/非字符串项）只能得到空记录，绝不能把异常抛出 domain。"""
    si = _SI(methods={"extract_filenames": lambda: [None, "   ", ""]})
    files, via, note = assets._import_record(_Obj(si))
    assert files == [] and via is None and note == "empty_record"


# ---------- 判档与排除 ----------

def _rec(*exists):
    return [{"path": "P%d" % i, "exists": bool(e)} for i, e in enumerate(exists)]


def test_src_verdict_five_states():
    assert assets._src_verdict([]) == "no_record"
    assert assets._src_verdict(_rec(True, True)) == "present"
    assert assets._src_verdict(_rec(False, False)) == "missing"
    assert assets._src_verdict(_rec(True, False)) == "partial"
    assert assets._src_verdict(_rec(False, True)) == "partial"


def test_src_scan_skip_excludes_editor_authored():
    assert assets._src_scan_skip("Texture2D", "/Game/T/A") is None
    assert assets._src_scan_skip("World", "/Game/Maps/LV_Main") == "level"
    assert assets._src_scan_skip("StaticMesh", "/Game/__ExternalActors__/X") == "external_wp"
    assert assets._src_scan_skip("StaticMesh", "/Game/__ExternalObjects__/X") == "external_wp"
    assert assets._src_scan_skip("Redirector", "/Game/A/Old") == "redirector"


def test_is_under_project_dir_normalizes_separators_both_sides():
    root = "C:\\Proj\\Game"                       # 引擎回的 project_dir 形态（反斜杠）
    assert assets._is_under("C:/Proj/Game/Content/T.png", root) is True
    assert assets._is_under("C:\\Proj\\game\\Content\\T.png", root) is True
    assert assets._is_under("C:/Proj/Other/T.png", root) is False
    assert assets._is_under("C:/Proj/Gameback/T.png", root) is False   # 前缀粘连不算在内
    assert assets._is_under("D:/Src/T.png", root) is False
    assert assets._is_under("C:/Whatever/T.png", None) is False        # 取不到工程根不猜


# ---------- 纯逻辑聚合（total/truncated/cap 纪律） ----------

def _row(pkg, verdict, size):
    return {"package": pkg, "class": "Texture2D", "uasset_bytes": size,
            "uasset_mb": round(size / 1048576.0, 3), "verdict": verdict,
            "via": "extract_filenames", "recorded": []}


def test_rollup_counts_and_missing_bytes_only_counts_full_loss():
    rows = [_row("/Game/T/A", "missing", 1000), _row("/Game/T/B", "partial", 2000),
            _row("/Game/T/C", "present", 4000), _row("/Game/M/D", "no_record", 500),
            _row("/Game/M/E", "load_failed", 600)]
    out = assets._source_rollup(rows)
    assert out["missing_count"] == 1 and out["partial_count"] == 1
    assert out["present_count"] == 1 and out["no_record"] == 1
    assert out["load_failed_count"] == 1
    assert out["with_record"] == 3                 # present+partial+missing
    assert out["missing_bytes"] == 1000            # partial 的体量不混进这本账
    assert out["missing_mb"] == round(1000 / 1048576.0, 3)


def test_rollup_dir_aggregation_sorted_by_missing_first():
    rows = [_row("/Game/Hot/A", "missing", 10), _row("/Game/Hot/B", "missing", 20),
            _row("/Game/Cool/C", "present", 99)]
    out = assets._source_rollup(rows)
    keys = list(out["by_dir"].keys())
    assert keys[0] == "/Game/Hot"                  # 缺料体量大的目录排前
    assert out["by_dir"]["/Game/Hot"]["missing"] == 2
    assert out["by_dir"]["/Game/Hot"]["missing_bytes"] == 30
    assert out["by_dir"]["/Game/Hot"]["bytes"] == 30
    assert out["by_dir"]["/Game/Cool"]["missing"] == 0
    assert out["by_dir_truncated"] is False


def test_rollup_truncates_rows_and_keeps_order():
    rows = [_row("/Game/T/Big", "missing", 5000), _row("/Game/T/Mid", "partial", 4000),
            _row("/Game/T/Small", "missing", 100), _row("/Game/T/P", "present", 9000)]
    out = assets._source_rollup(rows, max_items=2)
    assert [r["package"] for r in out["rows"]] == ["/Game/T/Big", "/Game/T/Small"]
    assert out["rows_truncated"] is True           # 先按判档(缺料优先)，再按体量降序
    out2 = assets._source_rollup(rows, max_items=0)   # <=0 = 不截断，别静默吞明细
    assert len(out2["rows"]) == 4 and out2["rows_truncated"] is False


def test_rollup_by_dir_cap_flags_truncation():
    rows = [_row("/Game/D%d/A" % i, "missing", 100 + i) for i in range(5)]
    out = assets._source_rollup(rows, by_dir_cap=3)
    assert len(out["by_dir"]) == 3 and out["by_dir_truncated"] is True


def test_clip_flattens_newlines_and_truncates():
    assert assets._clip("a\nb\rc", 120) == "a b c"
    assert assets._clip("x" * 30, 10) == "x" * 10 + "..."
