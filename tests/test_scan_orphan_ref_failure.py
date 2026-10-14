# -*- coding: utf-8 -*-
"""scan_orphan_assets 引用查询失败降级（5.4 真实工程实测：单包查询抛错不得带走整场扫描）。

夹具在编辑器会话里留下的 in-memory 复制靶（`/Game/PrismCalib/_pwtmp/copy_*`）注册在
AssetRegistry 里但磁盘上没有，对它调 get_referencers 会抛。旧代码在第一个包就
`return UE_API_MISMATCH`，整份孤儿清单消失——把一个包的古怪状态误判成引擎签名漂移。
约定：单包失败记 `ref_query_failed` 并按"引用者未知 = 不当孤儿"保守排除；
整窗都失败才报 UE_API_MISMATCH（绝不静默出"看似干净"的清单）。
"""
from __future__ import annotations

import os
import sys
import types

from prism import envelope
from prism.domain import assets as A


class _Data:
    def __init__(self, package, cls):
        self.package_name = package
        self.asset_class_path = "/Script/Engine." + cls
        self._obj = package + "." + package.rpartition("/")[2]

    def get_object_path(self):
        return self._obj

    def get_primary_asset_id(self):
        return "id"


def _mk(monkeypatch, tmp_path, datas, fail_pkgs=()):
    unreal = types.ModuleType("unreal")
    unreal.Name = lambda s: s
    content = os.path.join(str(tmp_path), "Content")
    unreal.Paths = types.SimpleNamespace(project_content_dir=lambda: content + "/")
    fail = set(fail_pkgs)

    def get_assets_by_path(name, *a):
        return list(datas)

    def get_referencers(name, *a):
        # get_assets_by_path/get_referencers 跨版本会传 Name 或 [Name]，两种形状都要判
        items = name if isinstance(name, (list, tuple)) else [name]
        if any(str(x) in fail for x in items):
            raise RuntimeError("Assertion: package is not on disk")
        return []

    ar = types.SimpleNamespace(get_assets_by_path=get_assets_by_path,
                               get_referencers=get_referencers)
    unreal.AssetRegistryHelpers = types.SimpleNamespace(get_asset_registry=lambda: ar)
    monkeypatch.setitem(sys.modules, "unreal", unreal)
    for pkg in ("A/Gone", "A/Kept"):
        rel = ("/Game/" + pkg)[len("/Game/"):].split("/")
        path = os.path.join(content, *rel) + ".uasset"
        d = os.path.dirname(path)
        if not os.path.isdir(d):
            os.makedirs(d)
        with open(path, "wb") as fh:
            fh.write(b"\\0" * 2048)


GOOD = "/Game/A/Kept"
GONE = "/Game/A/Gone"


def test_single_ref_query_failure_does_not_abort_scan(monkeypatch, tmp_path):
    _mk(monkeypatch, tmp_path, [_Data(GONE, "Texture2D"), _Data(GOOD, "Texture2D")],
        fail_pkgs=[GONE])
    res = A.scan_orphan_assets(folder="/Game", limit=50, cascade=False)
    assert res.get("ok") is not False, res
    assert res["ref_query_failed_count"] == 1, res["ref_query_failed_count"]
    assert res["ref_query_failed"] == [GONE], res["ref_query_failed"]
    assert GONE not in [o["package"] for o in res["orphans"]]  # 引用者未知 -> 保守不算孤儿
    assert [o["package"] for o in res["orphans"]] == [GOOD], res["orphans"]
    assert "查询失败" in res["note"]
    assert res["orphan_count"] == 1


def test_all_packages_failing_still_reports_api_mismatch(monkeypatch, tmp_path):
    _mk(monkeypatch, tmp_path, [_Data(GONE, "Texture2D"), _Data(GOOD, "Texture2D")],
        fail_pkgs=[GONE, GOOD])
    res = A.scan_orphan_assets(folder="/Game", limit=50, cascade=False)
    assert res.get("ok") is False, res
    assert res["error"]["code"] == envelope.Code.UE_API_MISMATCH, res["error"]
    assert "全部查询失败" in res["error"]["message"], res["error"]["message"]


def test_degrade_shape_keeps_new_keys():
    saved = sys.modules.pop("unreal", None)
    try:
        res = A.scan_orphan_assets(folder="/Game")
    finally:
        if saved is not None:
            sys.modules["unreal"] = saved
    assert {"ref_query_failed", "ref_query_failed_count",
            "ref_query_failed_truncated"}.issubset(res.keys())
    assert res["ref_query_failed_count"] == 0
