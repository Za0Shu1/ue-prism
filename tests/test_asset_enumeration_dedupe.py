# -*- coding: utf-8 -*-
"""多导出包去重回归（5.4 真实工程 ClientProject 实测缺陷）。

AssetRegistry `get_assets_by_path` 对**多导出包**（HLOD 烘焙图集、子资产、BP 生成类）
按 *导出* 返回多条 AssetData。真实工程里一个 HLOD 包以子导出身份重复 76 次，
把 limit=400 的纹理窗口吃掉 124 格、同包重复度量，还把子导出的类名当成本包的类；
而 `package.leaf` 代表的对象在该包里并不存在，导致读尺寸全部失败（125 个 failed）。
约定：一包只留一条代表导出（优先类匹配、最像主资产者），object_path 落在真实导出上。
"""
from __future__ import annotations

import os
import sys
import types

from prism.domain import assets as A
from prism.domain import optutil


class _Data:
    """AssetData 形状：package_name / asset_class_path(旧式 Name 字符串) / 导出对象路径。"""

    def __init__(self, package, cls, export=None, primary=True):
        self.package_name = package
        self.asset_class_path = "/Script/Engine." + cls
        leaf = package.rpartition("/")[2]
        self._export = package + "." + (leaf if export is None else export)
        self._primary = "id" if primary else "None"

    def get_object_path(self):
        return self._export

    def get_primary_asset_id(self):
        return self._primary


class _BlindData(_Data):
    """5.x 老形状：拿不到 object_path，也没有 asset_name，只能回退 package.leaf。"""

    def get_object_path(self):
        raise AttributeError("no object_path on this build")


def _touch(tmp_path, package, size):
    rel = package[len("/Game/"):]
    path = os.path.join(str(tmp_path), "Content", *rel.split("/")) + ".uasset"
    d = os.path.dirname(path)
    if not os.path.isdir(d):
        os.makedirs(d)
    with open(path, "wb") as fh:
        fh.write(b"\0" * size)
    return path


def _mk_unreal(monkeypatch, tmp_path, datas, referencers=None):
    unreal = types.ModuleType("unreal")
    unreal.Name = lambda s: s
    content = os.path.join(str(tmp_path), "Content")
    unreal.Paths = types.SimpleNamespace(project_content_dir=lambda: content + "/")

    def get_assets_by_path(name, *a):
        return list(datas)

    def get_referencers(name, *a):
        return list(referencers or [])

    ar = types.SimpleNamespace(get_assets_by_path=get_assets_by_path,
                               get_referencers=get_referencers)
    unreal.AssetRegistryHelpers = types.SimpleNamespace(get_asset_registry=lambda: ar)
    monkeypatch.setitem(sys.modules, "unreal", unreal)
    return unreal, ar


HLOD = "/Game/ClientLib/Level/Region/HLOD/Lv_GIS_0_HLOD"
PLAIN = "/Game/Textures/T_Ground_01"


def _hlod_datas():
    # 同一个包：1 个主导出（非纹理）+ 2 个烘焙图集导出（Texture2D）
    return [
        _Data(HLOD, "HLODStaticMeshComponent", export="Lv_GIS_0_HLOD"),
        _Data(HLOD, "MaterialInstance", export="Lv_GIS_0_HLOD_Mat"),
        _Data(HLOD, "Texture2D", export="Lv_GIS_0_HLOD_ATLAS0"),
        _Data(HLOD, "Texture2D", export="Lv_GIS_0_HLOD_ATLAS1"),
    ]


def test_dedupe_collapses_multi_export_package_to_one_row():
    datas = _hlod_datas()
    out = A._dedupe_by_package(datas, None)
    assert [p for p, _d in out] == [HLOD]


def test_dedupe_prefers_class_matching_export():
    datas = _hlod_datas()
    out = A._dedupe_by_package(datas, None, prefer_terms=("texture",))
    assert len(out) == 1
    pkg, data = out[0]
    assert A._class_name(None, data) == "Texture2D"
    assert A._export_object_name(None, data) == "Lv_GIS_0_HLOD_ATLAS0"


def test_export_object_path_falls_back_to_leaf_when_api_missing():
    data = _BlindData(PLAIN, "Texture2D")
    assert A._export_object_path(None, PLAIN, data) == PLAIN + ".T_Ground_01"
    assert A._export_object_path(None, PLAIN, _Data(PLAIN, "Texture2D")) == PLAIN + ".T_Ground_01"


def test_from_registry_yields_one_row_per_package_with_real_object_path(monkeypatch, tmp_path):
    _touch(tmp_path, HLOD, 10 * 1024)
    _touch(tmp_path, PLAIN, 30 * 1024)
    datas = _hlod_datas() + [_Data(PLAIN, "Texture2D")]
    unreal, ar = _mk_unreal(monkeypatch, tmp_path, datas)
    window, universe, note = optutil._from_registry(
        unreal, ar, "/Game", optutil.TEXTURE_TERMS, 10, 0)
    assert note == ""
    # 4 条 AssetData（3 个导出属于同一个包）-> universe 只数唯一的包
    assert universe == 2, universe
    assert len(window) == 2
    assert len(set(r["package"] for r in window)) == 2
    by_pkg = dict((r["package"], r) for r in window)
    hlod = by_pkg[HLOD]
    assert hlod["class"] == "Texture2D", hlod
    assert hlod["object_path"] == HLOD + ".Lv_GIS_0_HLOD_ATLAS0", hlod
    assert hlod["object_path"] != HLOD + ".Lv_GIS_0_HLOD"
    # 窗口按磁盘体量降序：30KB 的普通纹理在前
    assert window[0]["package"] == PLAIN


def test_limit_window_not_eaten_by_one_package(monkeypatch, tmp_path):
    _touch(tmp_path, HLOD, 9 * 1024)
    _touch(tmp_path, PLAIN, 30 * 1024)
    datas = _hlod_datas() * 20 + [_Data(PLAIN, "Texture2D")]
    unreal, ar = _mk_unreal(monkeypatch, tmp_path, datas)
    window, universe, _note = optutil._from_registry(
        unreal, ar, "/Game", optutil.TEXTURE_TERMS, 1, 0)
    assert universe == 2 and len(window) == 1
    assert window[0]["package"] == PLAIN


def test_non_matching_package_is_dropped_not_mislabeled(monkeypatch, tmp_path):
    _touch(tmp_path, HLOD, 9 * 1024)
    datas = [
        _Data(HLOD, "HLODStaticMeshComponent", export="Lv_GIS_0_HLOD"),
        _Data(HLOD, "MaterialInstance", export="Lv_GIS_0_HLOD_Mat"),
    ]
    unreal, ar = _mk_unreal(monkeypatch, tmp_path, datas)
    window, universe, _note = optutil._from_registry(
        unreal, ar, "/Game", optutil.TEXTURE_TERMS, 10, 0)
    assert window == [] and universe == 0


def test_scan_orphan_counts_packages_not_exports(monkeypatch, tmp_path):
    _touch(tmp_path, HLOD, 1024)
    _touch(tmp_path, PLAIN, 2048)
    datas = _hlod_datas() + [_Data(PLAIN, "Texture2D")]
    unreal, ar = _mk_unreal(monkeypatch, tmp_path, datas)
    # domain 函数返回裸 dict（bridge 才包信封）
    res = A.scan_orphan_assets(folder="/Game", limit=50, cascade=False)
    assert res["found_registry"] is True
    assert res["total_available"] == 2, res["total_available"]
    assert res["total_scanned"] == 2
    pkgs = set(o["package"] for o in res["orphans"])
    assert pkgs == set([HLOD, PLAIN]), pkgs
