"""UE 插件打包（pluginpack）无引擎测试：清单合法 / 闭包完整 / 装配产物正确 / 写边界。"""
import json
import os

import pytest

from prism import pluginpack as pp


def test_closure_modules_exist_in_installed_package():
    root = pp.prism_root()
    for rel in pp.ENGINE_MODULES:
        assert os.path.isfile(os.path.join(root, rel.replace("/", os.sep))), rel


def test_render_uplugin_is_valid_json_and_pure_python():
    doc = json.loads(pp.render_uplugin("9.9.9"))
    assert doc["VersionName"] == "9.9.9"
    assert doc["Modules"] == []            # 无 C++ 模块 -> 免编译
    assert doc["EnabledByDefault"] is True
    assert any(p["Name"] == "PythonScriptPlugin" and p["Enabled"] for p in doc["Plugins"])
    assert doc["CanContainContent"] is True   # 否则 UE 不扫本插件 Content/Python（bridge 不自启）


def test_init_unreal_does_not_import_forbidden():
    src = pp._init_unreal_source()
    for bad in ("import prism.server", "from prism.server", "mcp"):
        assert bad not in src


def test_plan_is_readonly_no_write(tmp_path):
    dest = str(tmp_path / "UEPrism")
    pl = pp.plan(dest, version="1.0.1")
    assert pl["exists"] is False
    assert len(pl["files"]) == 2 + len(pp.ENGINE_MODULES)
    assert not os.path.exists(dest)        # plan 绝不落盘


def test_assemble_builds_tree_without_server_side(tmp_path):
    dest = str(tmp_path / "UEPrism")
    r = pp.assemble(dest, version="1.0.1")
    assert r["ok"] and not r["conflict"]
    assert r["files_written"] == 2 + len(pp.ENGINE_MODULES)
    # uplugin 在位且合法
    up = os.path.join(dest, "UEPrism.uplugin")
    assert os.path.isfile(up)
    json.load(open(up, encoding="utf-8"))
    # init_unreal + 引擎模块落地
    assert os.path.isfile(os.path.join(dest, "Content", "Python", "init_unreal.py"))
    assert os.path.isfile(os.path.join(dest, "Content", "Python", "prism", "bridge.py"))
    assert os.path.isfile(os.path.join(dest, "Content", "Python", "prism", "domain", "migrate.py"))
    # 禁止项绝不进插件
    pyroot = os.path.join(dest, "Content", "Python", "prism")
    for bad in pp.FORBIDDEN_IN_PLUGIN:
        assert not os.path.exists(os.path.join(pyroot, bad)), bad
    # 无 __pycache__ 混入
    assert "__pycache__" not in os.listdir(pyroot)


def test_assemble_refuses_overwrite_without_force(tmp_path):
    dest = str(tmp_path / "UEPrism")
    pp.assemble(dest, version="1.0.1")
    r2 = pp.assemble(dest, version="1.0.1")           # 已存在，无 force
    assert r2["ok"] is False and r2["conflict"] is True
    r3 = pp.assemble(dest, version="1.0.2", force=True)
    assert r3["ok"] and r3["version"] == "1.0.2"


def test_assemble_missing_closure_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(pp, "prism_root", lambda: str(tmp_path))  # 假根，无模块
    with pytest.raises(FileNotFoundError):
        pp.assemble(str(tmp_path / "UEPrism"))