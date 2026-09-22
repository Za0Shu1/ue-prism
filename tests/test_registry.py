"""registry 单元测试（无引擎、无 mcp）：写指针 / 现场新鲜度发现 / 注销 / slug 唯一。见 DESIGN_v1.0 §3。"""
from __future__ import annotations

import json
import os
import time

from prism import bus, registry


def _mk(tmp_path, name):
    proj = tmp_path / name
    bdir = proj / "Saved" / "Prism"
    os.makedirs(str(bdir), exist_ok=True)
    return str(proj), str(bdir)


def _heartbeat(bdir, age=0.0):
    os.makedirs(bdir, exist_ok=True)
    with open(os.path.join(bdir, bus.HEARTBEAT_NAME), "w", encoding="utf-8") as f:
        json.dump({"ts": time.time() - age}, f)


def test_register_then_discover_live(tmp_path, monkeypatch):
    monkeypatch.setenv("PRISM_REGISTRY_DIR", str(tmp_path / "reg"))
    proj, bdir = _mk(tmp_path, "Alpha")
    path = registry.register(bdir, proj, ue_version="5.4.4")
    assert os.path.isfile(path)
    _heartbeat(bdir, age=0.0)
    live = registry.live_entries()
    assert len(live) == 1
    assert live[0]["slug"] == "Alpha" and live[0]["live"] is True
    assert live[0]["ue_version"] == "5.4.4"
    assert live[0]["project_dir"].endswith("Alpha")


def test_discover_stale_is_not_live(tmp_path, monkeypatch):
    monkeypatch.setenv("PRISM_REGISTRY_DIR", str(tmp_path / "reg"))
    proj, bdir = _mk(tmp_path, "Beta")
    registry.register(bdir, proj)
    _heartbeat(bdir, age=10000.0)  # 远超 HB_STALE_SECONDS
    assert registry.live_entries() == []
    disc = registry.discover()
    assert len(disc) == 1 and disc[0]["live"] is False


def test_unregister_removes_pointer(tmp_path, monkeypatch):
    monkeypatch.setenv("PRISM_REGISTRY_DIR", str(tmp_path / "reg"))
    proj, bdir = _mk(tmp_path, "Gamma")
    registry.register(bdir, proj)
    assert registry.find("Gamma") is not None
    registry.unregister(bdir)
    assert registry.find("Gamma") is None


def test_same_basename_projects_do_not_collide(tmp_path, monkeypatch):
    monkeypatch.setenv("PRISM_REGISTRY_DIR", str(tmp_path / "reg"))
    p1, b1 = _mk(tmp_path / "a", "Same")
    p2, b2 = _mk(tmp_path / "b", "Same")
    registry.register(b1, p1)
    registry.register(b2, p2)
    # 两条独立指针（文件名带 bus_dir hash），互不覆盖
    disc = registry.discover()
    assert len(disc) == 2
    assert len({d["bus_dir"] for d in disc}) == 2


def test_project_from_bus_roundtrip(tmp_path):
    proj = os.path.abspath(str(tmp_path / "ProjX"))
    bdir = os.path.join(proj, "Saved", "Prism")
    assert os.path.normcase(registry.project_from_bus(bdir)) == os.path.normcase(proj)


def _mk_with_uproject(tmp_path, folder, uproject):
    proj = tmp_path / folder
    bdir = proj / "Saved" / "Prism"
    os.makedirs(str(bdir), exist_ok=True)
    if uproject:
        with open(str(proj / (uproject + ".uproject")), "w", encoding="utf-8") as f:
            f.write("{}")
    return str(proj), str(bdir)


def test_project_name_prefers_uproject(tmp_path, monkeypatch):
    monkeypatch.setenv("PRISM_REGISTRY_DIR", str(tmp_path / "reg"))
    proj, bdir = _mk_with_uproject(tmp_path, "folderX", "RealName")
    registry.register(bdir, proj, ue_version="5.3.2")
    _heartbeat(bdir, age=0.0)
    disc = registry.discover()
    assert disc[0]["name"] == "RealName" and disc[0]["slug"] == "RealName"
    assert registry.find("RealName") is not None   # uproject 名可定位
    assert registry.find("folderX") is not None     # 文件夹名仍可定位


def test_multiple_uproject_falls_back_to_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("PRISM_REGISTRY_DIR", str(tmp_path / "reg"))
    proj = tmp_path / "twoProj"
    bdir = proj / "Saved" / "Prism"
    os.makedirs(str(bdir), exist_ok=True)
    for n in ("A.uproject", "B.uproject"):
        with open(str(proj / n), "w", encoding="utf-8") as f:
            f.write("{}")
    registry.register(str(bdir), str(proj))
    assert registry.discover()[0]["name"] == "twoProj"  # 有歧义 -> 回落文件夹名


def test_reproject_rename_purges_orphan_pointer(tmp_path, monkeypatch):
    """命名口径切换后，同 bus_dir 的旧名孤儿指针被清掉（不留双条）。"""
    monkeypatch.setenv("PRISM_REGISTRY_DIR", str(tmp_path / "reg"))
    proj = tmp_path / "libvlcue"
    bdir = proj / "Saved" / "Prism"
    os.makedirs(str(bdir), exist_ok=True)
    registry.register(str(bdir), str(proj))            # 无 uproject -> name=libvlcue
    assert registry.discover()[0]["name"] == "libvlcue"
    with open(str(proj / "VLC_Test.uproject"), "w", encoding="utf-8") as f:
        f.write("{}")
    registry.register(str(bdir), str(proj))            # 重注册 -> name=VLC_Test
    disc = registry.discover()
    assert len(disc) == 1 and disc[0]["name"] == "VLC_Test"
    assert registry.find("libvlcue")["name"] == "VLC_Test"  # 文件夹名仍路由到这条唯一指针# 旧名孤儿已清
