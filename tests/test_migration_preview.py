"""preview_asset_migration 单元测试（无引擎、桩化 BusClient）：目标构建/冲突/影响面/错误分支。"""
from __future__ import annotations

from prism import bus, envelope, server


class _FakeClient(object):
    def __init__(self, responses, **kw):
        self._r = responses
        self.calls = []

    # 适配 server.bus.BusClient(bus_dir, timeout=...) 构造
    def __call__(self, *a, **k):  # pragma: no cover - 兼容误用
        return self

    def call(self, fn, args):
        self.calls.append((fn, args.get("asset_path")))
        key = (fn, args.get("asset_path"))
        if key in self._r:
            return self._r[key]
        return envelope.make_err("RUNTIME_ERROR", "unexpected call %s" % (key,))


def _install(monkeypatch, responses):
    monkeypatch.setenv("PRISM_BUS_DIR", "D:/P/Saved/Prism")  # 走显式配置分支，返回固定 bus_dir
    holder = {}

    def _ctor(bus_dir, timeout=None):
        c = _FakeClient(responses)
        holder["c"] = c
        return c

    monkeypatch.setattr(bus, "BusClient", _ctor)
    return holder


def _found():
    return envelope.make_ok({"found": True, "class": "Material", "package_name": "/Game/M"})


def _notfound():
    return envelope.make_ok({"found": False, "note": "asset_not_found_in_registry"})


def _refs(n=3):
    return envelope.make_ok({
        "found": True, "direction": "used_by", "used_by": ["/Game/U%d" % i for i in range(n)],
        "used_by_total": n, "uses_total": 0, "uses": [], "truncated": False, "cap": 200,
    })


def test_rename_plan_ok(monkeypatch):
    _install(monkeypatch, {
        ("describe_asset", "/Game/Mat/M_A"): _found(),
        ("describe_asset", "/Game/Mat/M_B"): _notfound(),   # 目标不存在 -> 无冲突
        ("get_asset_references", "/Game/Mat/M_A"): _refs(5),
    })
    env = server.preview_asset_migration("/Game/Mat/M_A", new_name="M_B")
    assert env["ok"] is True
    r = env["result"]
    assert r["dry_run"] is True
    assert r["action"] == ["rename"]
    assert r["target"] == "/Game/Mat/M_B"
    assert r["conflict"] is False
    assert r["impact"]["referencer_count"] == 5
    assert len(r["impact"]["referencers"]) == 5


def test_conflict_detected(monkeypatch):
    _install(monkeypatch, {
        ("describe_asset", "/Game/Mat/M_A"): _found(),
        ("describe_asset", "/Game/Mat/M_B"): _found(),       # 目标已存在 -> 冲突
        ("get_asset_references", "/Game/Mat/M_A"): _refs(1),
    })
    env = server.preview_asset_migration("/Game/Mat/M_A", new_name="M_B")
    assert env["ok"] is True
    assert env["result"]["conflict"] is True


def test_move_plan_target(monkeypatch):
    _install(monkeypatch, {
        ("describe_asset", "/Game/Char/M_Weapon"): _found(),
        ("describe_asset", "/Game/Shared/M_Weapon"): _notfound(),
        ("get_asset_references", "/Game/Char/M_Weapon"): _refs(0),
    })
    env = server.preview_asset_migration("/Game/Char/M_Weapon", dest_path="/Game/Shared")
    r = env["result"]
    assert r["action"] == ["move"]
    assert r["target"] == "/Game/Shared/M_Weapon"
    assert r["conflict"] is False
    assert r["impact"]["referencer_count"] == 0


def test_move_and_rename_target(monkeypatch):
    _install(monkeypatch, {
        ("describe_asset", "/Game/A/X"): _found(),
        ("describe_asset", "/Game/B/Y"): _notfound(),
        ("get_asset_references", "/Game/A/X"): _refs(2),
    })
    env = server.preview_asset_migration("/Game/A/X", dest_path="/Game/B", new_name="Y")
    assert env["result"]["action"] == ["rename", "move"]
    assert env["result"]["target"] == "/Game/B/Y"


def test_bad_path_rejected(monkeypatch):
    _install(monkeypatch, {})
    env = server.preview_asset_migration("/Engine/Things", new_name="Z")
    assert env["ok"] is False and env["error"]["code"] == "RUNTIME_ERROR"


def test_no_target_rejected(monkeypatch):
    _install(monkeypatch, {})
    env = server.preview_asset_migration("/Game/Mat/M_A")
    assert env["ok"] is False and "new_name" in env["error"]["message"]


def test_source_not_found(monkeypatch):
    _install(monkeypatch, {("describe_asset", "/Game/Mat/Nope"): _notfound()})
    env = server.preview_asset_migration("/Game/Mat/Nope", new_name="X")
    assert env["ok"] is False and "未找到" in env["error"]["message"]


def test_propagates_bridge_error(monkeypatch):
    _install(monkeypatch, {
        ("describe_asset", "/Game/Mat/M_A"): envelope.make_err("UE_API_MISMATCH", "AssetRegistry unavailable"),
    })
    env = server.preview_asset_migration("/Game/Mat/M_A", new_name="X")
    assert env["ok"] is False and env["error"]["code"] == "UE_API_MISMATCH"


def test_no_project_resolves_err(monkeypatch):
    monkeypatch.delenv("PRISM_PROJECT_DIR", raising=False)
    monkeypatch.delenv("PRISM_BUS_DIR", raising=False)
    server._CFG["bus_dir"] = None
    server._CFG["project_dir"] = None
    monkeypatch.setenv("PRISM_REGISTRY_DIR", "D:/nonexistent-reg-for-test")
    env = server.preview_asset_migration("/Game/Mat/M_A", new_name="X")
    assert env["ok"] is False and env["error"]["code"] == "NO_PROJECT"