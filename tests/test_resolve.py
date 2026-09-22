"""server._resolve 判定阶梯 + list_projects 发现测试（无引擎、无 mcp）。见 DESIGN_v1.0 §3.3。"""
from __future__ import annotations

import json
import os
import time

from prism import bus, registry, server


def _fresh_env(monkeypatch, tmp_path):
    # 清掉“显式配置”，强制走 registry 发现分支
    monkeypatch.delenv("PRISM_PROJECT_DIR", raising=False)
    monkeypatch.delenv("PRISM_BUS_DIR", raising=False)
    server._CFG["bus_dir"] = None
    server._CFG["project_dir"] = None
    monkeypatch.setenv("PRISM_REGISTRY_DIR", str(tmp_path / "reg"))


def _register_live(tmp_path, name, age=0.0):
    proj = tmp_path / name
    bdir = proj / "Saved" / "Prism"
    os.makedirs(str(bdir), exist_ok=True)
    registry.register(str(bdir), str(proj), ue_version="5.4.4")
    with open(os.path.join(str(bdir), bus.HEARTBEAT_NAME), "w", encoding="utf-8") as f:
        json.dump({"ts": time.time() - age}, f)
    return str(proj), str(bdir)


def test_explicit_config_shortcircuits(monkeypatch, tmp_path):
    monkeypatch.delenv("PRISM_REGISTRY_DIR", raising=False)
    monkeypatch.setenv("PRISM_PROJECT_DIR", str(tmp_path / "Explicit"))
    server._CFG["bus_dir"] = None
    server._CFG["project_dir"] = None
    bus_dir, pd, err = server._resolve()
    assert err is None
    assert os.path.normcase(pd) == os.path.normcase(str(tmp_path / "Explicit"))
    assert bus_dir.endswith(os.path.join("Saved", "Prism"))


def test_single_live_autoselects(monkeypatch, tmp_path):
    _fresh_env(monkeypatch, tmp_path)
    proj, bdir = _register_live(tmp_path, "Only")
    bus_dir, pd, err = server._resolve()
    assert err is None
    assert bus_dir == os.path.abspath(bdir).replace("\\", "/")
    assert pd.endswith("Only")


def test_no_project_when_empty(monkeypatch, tmp_path):
    _fresh_env(monkeypatch, tmp_path)
    _b, _p, err = server._resolve()
    assert err is not None and err["error"]["code"] == "NO_PROJECT"


def test_ambiguous_when_multi_live(monkeypatch, tmp_path):
    _fresh_env(monkeypatch, tmp_path)
    _register_live(tmp_path, "One")
    _register_live(tmp_path, "Two")
    _b, _p, err = server._resolve()
    assert err is not None and err["error"]["code"] == "AMBIGUOUS_PROJECT"
    msg = err["error"]["message"]
    assert "One" in msg and "Two" in msg  # 候选清单要回显给用户，方便其传 project=


def test_project_selector_picks_among_many(monkeypatch, tmp_path):
    _fresh_env(monkeypatch, tmp_path)
    _register_live(tmp_path, "One")
    proj2, bdir2 = _register_live(tmp_path, "Two")
    bus_dir, pd, err = server._resolve(project="Two")
    assert err is None and pd.endswith("Two") and bus_dir == os.path.abspath(bdir2).replace("\\", "/")


def test_project_selector_not_found(monkeypatch, tmp_path):
    _fresh_env(monkeypatch, tmp_path)
    _register_live(tmp_path, "Real")
    _b, _p, err = server._resolve(project="Ghost")
    assert err is not None and err["error"]["code"] == "PROJECT_NOT_FOUND"


def test_bridge_tool_rejects_offline_but_cook_allows_stale(monkeypatch, tmp_path):
    _fresh_env(monkeypatch, tmp_path)
    _register_live(tmp_path, "Closed", age=10000.0)  # 注册在，但编辑器关了（心跳陈旧）
    _b, _p, err = server._resolve(project=None)  # 桥工具默认严格：无活跃
    assert err is not None and err["error"]["code"] == "NO_PROJECT"
    _b2, p2, err2 = server._resolve(project=None, allow_stale=True)  # cook 允许关编辑器
    assert err2 is None and p2.endswith("Closed")


def test_list_projects_reports_liveness(monkeypatch, tmp_path):
    _fresh_env(monkeypatch, tmp_path)
    _register_live(tmp_path, "Alive", age=0.0)
    _register_live(tmp_path, "Gone", age=10000.0)
    env = server.list_projects()
    assert env["ok"] is True
    r = env["result"]
    assert r["total"] == 2 and r["live"] == 1
    names = {p["name"] for p in r["projects"]}
    assert names == {"Alive", "Gone"}


def test_project_selector_overrides_explicit_pin(monkeypatch, tmp_path):
    """v1.1 加固：设了默认 pin，调用方显式 project= 仍能点名连另一个工程。"""
    monkeypatch.setenv("PRISM_PROJECT_DIR", str(tmp_path / "PinnedDefault"))
    server._CFG["bus_dir"] = None
    server._CFG["project_dir"] = None
    monkeypatch.setenv("PRISM_REGISTRY_DIR", str(tmp_path / "reg"))
    _proj2, bdir2 = _register_live(tmp_path, "Two")
    bus_dir, pd, err = server._resolve(project="Two")
    assert err is None and pd.endswith("Two")
    assert bus_dir == os.path.abspath(bdir2).replace("\\", "/")


def test_project_selector_not_found_beats_pin(monkeypatch, tmp_path):
    """点名的工程注册表里没有 -> 明确 PROJECT_NOT_FOUND，绝不静默回落到 pin。"""
    monkeypatch.setenv("PRISM_PROJECT_DIR", str(tmp_path / "PinnedDefault"))
    server._CFG["bus_dir"] = None
    server._CFG["project_dir"] = None
    monkeypatch.setenv("PRISM_REGISTRY_DIR", str(tmp_path / "reg"))
    _register_live(tmp_path, "Real")
    _b, _p, err = server._resolve(project="Ghost")
    assert err is not None and err["error"]["code"] == "PROJECT_NOT_FOUND"
