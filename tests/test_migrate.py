"""PR-4 迁移写操作测试（无引擎）：server 双钥契约 + 冲突/源缺失拒绝；domain 守卫与降级。"""
from __future__ import annotations

from prism import bus, envelope
from prism import server
from prism.domain import migrate


class _Fake(object):
    def __init__(self, responses):
        self._r = responses
        self.calls = []

    def call(self, fn, args):
        self.calls.append((fn, args.get("asset_path")))
        return self._r.get((fn, args.get("asset_path")),
                           envelope.make_err("RUNTIME_ERROR", "unexpected %s" % ((fn, args.get("asset_path")),)))


def _install(monkeypatch, responses):
    monkeypatch.setenv("PRISM_BUS_DIR", "D:/P/Saved/Prism")
    holder = {}

    def _ctor(bus_dir, timeout=None):
        f = _Fake(responses)
        holder["c"] = f
        return f

    monkeypatch.setattr(bus, "BusClient", _ctor)
    return holder


def _found():
    return envelope.make_ok({"found": True, "class": "Material", "package_name": "/Game/A/X"})


def _notfound():
    return envelope.make_ok({"found": False, "note": "asset_not_found_in_registry"})


def _refs(n=2):
    return envelope.make_ok({"found": True, "direction": "used_by", "used_by": ["/Game/U%d" % i for i in range(n)],
                             "used_by_total": n, "truncated": False, "cap": 200})


def test_rename_default_dryrun_not_executed(monkeypatch):
    h = _install(monkeypatch, {
        ("describe_asset", "/Game/A/X"): _found(),
        ("describe_asset", "/Game/A/Y"): _notfound(),
        ("get_asset_references", "/Game/A/X"): _refs(),
    })
    env = server.migrate_asset_rename("/Game/A/X", new_name="Y")
    assert env["ok"] is True and env["result"]["dry_run"] is True
    assert env["result"]["would_execute"] == "rename" and env["result"]["target"] == "/Game/A/Y"
    assert ("migrate_asset_rename", "/Game/A/X") not in h["c"].calls  # 默认绝不落盘


def test_dryrun_false_confirm_false_still_not_executed(monkeypatch):
    h = _install(monkeypatch, {
        ("describe_asset", "/Game/A/X"): _found(),
        ("describe_asset", "/Game/A/Y"): _notfound(),
        ("get_asset_references", "/Game/A/X"): _refs(),
    })
    env = server.migrate_asset_rename("/Game/A/X", "Y", dry_run=False, confirm=False)
    assert env["ok"] is True and env["result"]["dry_run"] is True
    assert ("migrate_asset_rename", "/Game/A/X") not in h["c"].calls  # 缺 confirm 也不执行


def test_double_key_executes_rename(monkeypatch):
    h = _install(monkeypatch, {
        ("describe_asset", "/Game/A/X"): _found(),
        ("describe_asset", "/Game/A/Y"): _notfound(),
        ("migrate_asset_rename", "/Game/A/X"): envelope.make_ok({"op": "rename", "executed": True, "success": True, "api": "rename_asset(object,object)"}),
    })
    env = server.migrate_asset_rename("/Game/A/X", "Y", dry_run=False, confirm=True)
    assert env["ok"] is True and env["result"]["executed"] is True and env["result"]["success"] is True
    assert ("migrate_asset_rename", "/Game/A/X") in h["c"].calls
    # 真执行路径不查影响面（省成本）：未调用 get_asset_references
    assert ("get_asset_references", "/Game/A/X") not in h["c"].calls


def test_conflict_refuses_even_with_double_key(monkeypatch):
    h = _install(monkeypatch, {
        ("describe_asset", "/Game/A/X"): _found(),
        ("describe_asset", "/Game/A/Y"): _found(),  # 目标已存在
    })
    env = server.migrate_asset_rename("/Game/A/X", "Y", dry_run=False, confirm=True)
    assert env["ok"] is False and "目标已存在" in env["error"]["message"]
    assert ("migrate_asset_rename", "/Game/A/X") not in h["c"].calls


def test_move_double_key_executes(monkeypatch):
    h = _install(monkeypatch, {
        ("describe_asset", "/Game/A/X"): _found(),
        ("describe_asset", "/Game/B/X"): _notfound(),
        ("migrate_asset_move", "/Game/A/X"): envelope.make_ok({"op": "move", "executed": True, "success": True}),
    })
    env = server.migrate_asset_move("/Game/A/X", dest_path="/Game/B", dry_run=False, confirm=True)
    assert env["ok"] is True and env["result"]["success"] is True
    assert ("migrate_asset_move", "/Game/A/X") in h["c"].calls


def test_move_source_missing_refuses(monkeypatch):
    _install(monkeypatch, {("describe_asset", "/Game/A/Nope"): _notfound()})
    env = server.migrate_asset_move("/Game/A/Nope", dest_path="/Game/B", dry_run=False, confirm=True)
    assert env["ok"] is False and "源资产未找到" in env["error"]["message"]


def test_domain_rename_requires_confirm():
    env = migrate.migrate_asset_rename("/Game/A/X", "Y")  # confirm 默认 False
    assert env["ok"] is False and env["error"]["code"] == "RUNTIME_ERROR" and "confirm" in env["error"]["message"]


def test_domain_move_requires_confirm():
    env = migrate.migrate_asset_move("/Game/A/X", "/Game/B")
    assert env["ok"] is False and "confirm" in env["error"]["message"]


def test_domain_rename_no_engine_degrade():
    # confirm=True 但无 unreal -> 安全降级 executed=False / note=no_engine，绝不假装成功
    env = migrate.migrate_asset_rename("/Game/A/X", "Y", confirm=True)
    assert env["ok"] is True and env["result"]["executed"] is False
    assert env["result"]["note"] == "no_engine"


def test_domain_move_no_engine_degrade():
    env = migrate.migrate_asset_move("/Game/A/X", "/Game/B", confirm=True)
    assert env["ok"] is True and env["result"]["executed"] is False and env["result"]["note"] == "no_engine"


class _FakeData:
    def __init__(self, cls):
        self.asset_class_name = cls
    def is_valid(self):
        return True


class _FakeAR:
    def __init__(self, data):
        self._data = data
    def get_asset_by_object_path(self, _p):
        return self._data


class _FakeUnreal:
    class Name:
        def __new__(cls, s):
            return s


def test_target_exists_ignores_redirector(monkeypatch):
    from prism.domain import assets as _A
    from prism.domain import migrate as _M
    monkeypatch.setattr(_A, "_registry", lambda: _FakeAR(_FakeData("ObjectRedirector")))
    assert _M._target_exists(_FakeUnreal, "/Game/A/X.X") is False  # 改名遗留桩 -> 可覆盖
    monkeypatch.setattr(_A, "_registry", lambda: _FakeAR(_FakeData("StaticMesh")))
    assert _M._target_exists(_FakeUnreal, "/Game/A/X.X") is True   # 真资产 -> 视为占用



def test_bfs_closure_levels_dedup_and_cap():
    from prism.domain import assets as A
    graph = {
        "/Game/Root": (["/Game/A", "/Game/B"], "x"),
        "/Game/A": (["/Game/C"], "x"),
        "/Game/B": (["/Game/C", "/Game/D"], "x"),
        "/Game/C": ([], "x"),
        "/Game/D": (["/Game/Root", "/Game/A"], "x"),
    }

    def hop(pkg):
        return graph.get(pkg, ([], None))
    nodes, total, truncated, depth, sig, dep_map = A._bfs_closure(hop, "/Game/Root", 2000, 0)
    pkgs = [n["package"] for n in nodes]
    assert "/Game/C" in pkgs and "/Game/D" in pkgs
    assert pkgs.count("/Game/C") == 1
    assert "/Game/Root" not in pkgs
    assert total == 4
    assert sorted(dep_map["/Game/Root"]) == ["/Game/A", "/Game/B"]
    assert dep_map["/Game/D"] == ["/Game/Root", "/Game/A"]  # 回边在邻接表留痕，供 SCC 判环
    lv = {}
    for n in nodes:
        lv[n["package"]] = n["level"]
    assert lv["/Game/A"] == 1 and lv["/Game/C"] == 2
    n2, total2, trunc2, d2, s2, _dm2 = A._bfs_closure(hop, "/Game/Root", 1, 0)
    assert total2 == 1 and trunc2 is True


def test_asset_chain_no_engine_degrade():
    from prism.domain import assets as A
    r = A.get_asset_chain("/Game/A/B")
    assert isinstance(r, dict) and r.get("note") == "no_engine" and r["total"] == 0
    assert r["cycles"] == [] and r["cyclic"] is False and r["cycles_truncated"] is False
    assert r["god_assets"] == [] and r["god_scan"] is None and r["impact_summary"] is None



def test_migrate_asset_requires_confirm_for_write():
    env = migrate.migrate_asset("/Game/A/B", "/Game/Copy", dry_run=False, confirm=False)
    assert env["ok"] is False and env["error"]["code"] == "RUNTIME_ERROR" and "confirm" in env["error"]["message"]


def test_migrate_asset_rejects_non_game():
    env = migrate.migrate_asset("StarterContent/A", "/Game/Copy")
    assert env["ok"] is False and "Game" in env["error"]["message"]


def test_migrate_asset_no_engine_dryrun_degrade():
    env = migrate.migrate_asset("/Game/A/B", "/Game/Copy")
    assert env["ok"] is True and env["result"]["note"] == "no_engine" and env["result"]["executed"] is False



def test_dup_result_success_detection():
    from prism.domain import migrate as M
    assert M._dup_result(None) == (False, None)
    assert M._dup_result(False) == (False, None)
    assert M._dup_result(True) == (True, None)
    assert M._dup_result("/Game/X/X") == (True, "/Game/X/X")
    assert M._dup_result("") == (False, None)

    class _O:
        def get_path_name(self):
            return "/Game/MIG/X.X"
    ok, p = M._dup_result(_O())
    assert ok is True and p == "/Game/MIG/X.X"

    class _Bad:
        def get_path_name(self):
            raise RuntimeError("x")
    assert M._dup_result(_Bad())[0] is True
