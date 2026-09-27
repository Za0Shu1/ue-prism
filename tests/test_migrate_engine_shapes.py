# -*- coding: utf-8 -*-
"""5.4 真机形状回归：EditorAssetLibrary 缺 move_asset 时的兜底与诚实报错。

背景：手动真机测发现 migrate_asset_move 裸访问 E.move_asset 直接 AttributeError
（5.4 EAL 无此属性，此前离线 mock 从未覆盖该形状）。约定：属性缺失走 rename_asset
全路径兜底；连 rename_asset 都没有才报 UE_API_MISMATCH——绝不裸崩、绝不假装成功。
"""
from __future__ import annotations

import sys
import types

from prism import envelope
from prism.domain import migrate


def _mk_unreal(monkeypatch, attrs, log):
    unreal = types.ModuleType("unreal")

    def _mk(name, fn):
        def wrapper(*a, **k):
            log.append(name)
            return fn(*a, **k)
        return wrapper

    ns = {k: _mk(k, v) for k, v in attrs.items()}
    ns["load_asset"] = staticmethod(lambda p: None)
    unreal.EditorAssetLibrary = type("EditorAssetLibrary", (), ns)
    ar = types.SimpleNamespace(get_asset_by_object_path=lambda *a: None)
    unreal.AssetRegistryHelpers = types.SimpleNamespace(get_asset_registry=lambda: ar)
    unreal.Name = lambda s: s
    monkeypatch.setitem(sys.modules, "unreal", unreal)
    return unreal


def test_move_falls_back_to_rename_asset(monkeypatch):
    log = []
    _mk_unreal(monkeypatch, {"rename_asset": lambda a, b: True}, log)
    env = migrate.migrate_asset_move("/Game/P/X", "/Game/P/Sub", confirm=True)
    assert env["ok"] is True
    res = env["result"]
    assert res["success"] is True and res["executed"] is True
    assert res["api"].startswith("rename_asset->move"), res["api"]
    assert "rename_asset" in str(log)
    assert "move_asset" not in str(log)


def test_move_prefers_native_move_asset(monkeypatch):
    log = []
    _mk_unreal(monkeypatch,
               {"move_asset": lambda a, b: True,
                "rename_asset": lambda a, b: True}, log)
    env = migrate.migrate_asset_move("/Game/P/X", "/Game/P/Sub", confirm=True)
    res = env["result"]
    assert res["success"] is True
    assert res["api"].startswith("move_asset"), res["api"]


def test_move_all_api_missing_is_ue_api_mismatch(monkeypatch):
    log = []
    _mk_unreal(monkeypatch, {}, log)  # EAL 既无 move_asset 也无 rename_asset
    env = migrate.migrate_asset_move("/Game/P/X", "/Game/P/Sub", confirm=True)
    assert env["ok"] is False
    assert env["error"]["code"] == envelope.Code.UE_API_MISMATCH
    assert "AttributeMissing" in env["error"]["message"]


def test_rename_missing_attr_is_ue_api_mismatch(monkeypatch):
    log = []
    _mk_unreal(monkeypatch, {}, log)
    env = migrate.migrate_asset_rename("/Game/P/X", "Y", confirm=True)
    assert env["ok"] is False
    assert env["error"]["code"] == envelope.Code.UE_API_MISMATCH


def test_move_no_raw_attributeerror_when_only_candidates_fail(monkeypatch):
    log = []
    def boom(a, b):
        raise RuntimeError("editor busy")
    _mk_unreal(monkeypatch, {"rename_asset": boom}, log)
    env = migrate.migrate_asset_move("/Game/P/X", "/Game/P/Sub", confirm=True)
    assert env["ok"] is True  # 结构化的 attempted-but-not-confirmed，不是崩溃
    res = env["result"]
    assert res["executed"] is True and res["success"] is False
    assert any(t.get("err") == "RuntimeError" for t in res["tried"])
