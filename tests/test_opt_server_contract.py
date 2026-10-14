"""v1.5 优化族的 server 层契约：参数归一、双钥转发、导出只落方案文件。

协议层绝不碰引擎（不 import unreal），这里钉住它把 MCP 送来的字符串 bool
归一成真空值后再交给 domain——双钥契约最容易在这层漏。
"""
from __future__ import annotations

import json
import os

from prism import bus, envelope, server
from prism.domain import opt_instancing

NEW_TOOLS = ("scan_texture_streaming", "opt_texture_set_virtual",
             "opt_texture_clamp_max_size", "opt_texture_set_compression",
             "scan_nanite_candidates", "opt_mesh_set_nanite",
             "fix_nanite_translucent", "analyze_instancing",
             "opt_collapse_hism")


class _Fake(object):
    def __init__(self, response=None):
        self.calls = []
        self.response = response or envelope.make_ok({"op": "x", "targets": []})

    def call(self, fn, args):
        self.calls.append((fn, args))
        return self.response


def _install(monkeypatch, response=None):
    monkeypatch.setenv("PRISM_BUS_DIR", "D:/P/Saved/Prism")
    holder = {"all": []}

    def _ctor(bus_dir, timeout=None):
        fake = _Fake(response)
        fake.timeout = timeout
        holder["c"] = fake
        holder.setdefault("all", []).append(fake)
        return fake

    monkeypatch.setattr(bus, "BusClient", _ctor)
    return holder


def _ok(result):
    return envelope.make_ok(result)


# ---------- 注册契约 ----------

def test_new_tools_are_registered():
    names = {t.__name__ for t in server._TOOLS}
    assert set(NEW_TOOLS) <= names
    assert len(server._TOOLS) == 30


# ---------- 只读工具转发 ----------

def test_scan_texture_streaming_forwards_scope_and_limits(monkeypatch):
    h = _install(monkeypatch, _ok({"items": [], "summary": {"pool_demand_mb": 0.0}}))
    env = server.scan_texture_streaming(folder="/Game/Tex", map="/Game/Main",
                                        max_assets=50, min_mb=1.5, top=4, offset=10)
    fn, args = h["c"].calls[0]
    assert env["ok"] is True and fn == "scan_texture_streaming"
    assert args == {"folder": "/Game/Tex", "map": "/Game/Main", "paths": None,
                    "max_assets": 50, "min_mb": 1.5, "top": 4, "offset": 10,
                    "probe_source": True}
    assert h["c"].timeout >= 240          # 批量度量要走编辑器主线程，超时给足


def test_scan_texture_streaming_probe_source_off_forwards_bool(monkeypatch):
    """probe_source 必须归一成 bool 再下总线：脏字符串 "false" 不能被 domain 当真值。"""
    h = _install(monkeypatch, _ok({"items": []}))
    server.scan_texture_streaming(probe_source="false")
    _fn, args = h["c"].calls[0]
    assert args["probe_source"] is False
    h2 = _install(monkeypatch, _ok({"items": []}))
    server.scan_texture_streaming(probe_source=True)
    assert h2["c"].calls[0][1]["probe_source"] is True


def test_scan_nanite_candidates_forwards_thresholds(monkeypatch):
    h = _install(monkeypatch, _ok({"items": []}))
    server.scan_nanite_candidates(folder="/Game/M", min_triangles=123456,
                                  max_assets=9, top=2, offset=3)
    _fn, args = h["c"].calls[0]
    assert args["min_triangles"] == 123456 and args["max_assets"] == 9
    assert args["top"] == 2 and args["offset"] == 3


# ---------- 写工具的双钥转发 ----------

def test_write_tools_default_to_dry_run(monkeypatch):
    for name, fn in (("opt_texture_set_virtual", server.opt_texture_set_virtual),
                     ("opt_texture_clamp_max_size", server.opt_texture_clamp_max_size),
                     ("opt_texture_set_compression", server.opt_texture_set_compression),
                     ("opt_mesh_set_nanite", server.opt_mesh_set_nanite),
                     ("fix_nanite_translucent", server.fix_nanite_translucent)):
        h = _install(monkeypatch, _ok({"op": name}))
        fn(folder="/Game/X")
        _fn, args = h["c"].calls[0]
        assert args["dry_run"] is True and args["confirm"] is False, name


def test_string_falses_are_not_truthy_on_the_wire(monkeypatch):
    # MCP 通道会把 bool 送成 "False"/"0"：直接 if x 判断恒为真，必须归一
    h = _install(monkeypatch, _ok({"op": "x"}))
    server.opt_texture_clamp_max_size(folder="/Game/X", max_size=2048,
                                      dry_run="false", confirm="0")
    _fn, args = h["c"].calls[0]
    assert args["dry_run"] is False and args["confirm"] is False
    assert args["only_if_larger"] is True            # 未传时保持默认
    h2 = _install(monkeypatch, _ok({"op": "x"}))
    server.opt_mesh_set_nanite(folder="/Game/X", enabled="0",
                               skip_translucent="no", dry_run="0", confirm="on")
    _fn, args2 = h2["c"].calls[0]
    assert args2["enabled"] is False and args2["skip_translucent"] is False
    assert args2["dry_run"] is False and args2["confirm"] is True


def test_bad_bool_string_errors_without_touching_bus(monkeypatch):
    h = _install(monkeypatch, _ok({"op": "x"}))
    env = server.opt_texture_set_virtual(folder="/Game/X", dry_run="maybe")
    assert env["ok"] is False and env["error"]["code"] == envelope.Code.RUNTIME_ERROR
    assert h["all"] == []                            # 连总线客户端都没建：脏值不进引擎


def test_write_errors_from_domain_are_passed_through(monkeypatch):
    err = envelope.make_err(envelope.Code.RUNTIME_ERROR, "写操作：需 dry_run=False 且 confirm=True（双钥）")
    _install(monkeypatch, err)
    env = server.fix_nanite_translucent(folder="/Game/X", dry_run=False, confirm=False)
    assert env["ok"] is False and "双钥" in env["error"]["message"]


# ---------- analyze_instancing：只读 + export 只写方案文件 ----------

def test_analyze_instancing_default_does_not_write_files(monkeypatch, tmp_path):
    bus_dir = tmp_path / "Prism"
    bus_dir.mkdir()
    plan = {"summary": {"collapsible_actors": 170}, "groups": [{"mesh": "/Game/M/A",
                                                                "actors": 171}],
            "plan": {"strategy": "plan_only"}, "world_total_actors": 2096}
    monkeypatch.setenv("PRISM_BUS_DIR", str(bus_dir))
    holder = {}

    def _ctor(b, timeout=None):
        fake = _Fake(_OK_PLAN(plan))
        holder["c"] = fake
        return fake

    monkeypatch.setattr(bus, "BusClient", _ctor)
    env = server.analyze_instancing(min_count=5, cap=10)
    assert env["ok"] is True and "export" not in env["result"]
    assert not (bus_dir / "plans").exists()          # 默认绝不在盘上留东西
    _fn, args = holder["c"].calls[0]
    assert args["min_count"] == 5 and args["cap"] == 10


def _OK_PLAN(result):
    return envelope.make_ok(result)


def test_analyze_instancing_export_writes_atomic_plan(tmp_path, monkeypatch):
    bus_dir = tmp_path / "Prism"
    bus_dir.mkdir()
    plan = {"summary": {"collapsible_actors": 170}, "groups": [{"mesh": "/Game/M/A"}],
            "plan": {"strategy": "plan_only"}, "world_total_actors": 2096}
    monkeypatch.setenv("PRISM_BUS_DIR", str(bus_dir))
    monkeypatch.setattr(bus, "BusClient",
                        lambda b, timeout=None: _Fake(_OK_PLAN(plan)))
    env = server.analyze_instancing(export=True)
    exp = env["result"]["export"]
    assert exp["ok"] is True and "只落方案" in exp["note"]
    path = exp["path"]
    assert os.path.isfile(path) and os.path.dirname(path) == str(bus_dir / "plans")
    with open(path, "r", encoding="utf-8") as fh:
        payload = json.load(fh)
    assert payload["summary"]["collapsible_actors"] == 170
    assert payload["plan"]["strategy"] == "plan_only"
    assert not list((bus_dir / "plans").glob("*.tmp"))   # 原子写：不留半截文件


def test_analyze_instancing_export_failure_is_reported_not_thrown(tmp_path, monkeypatch):
    not_a_dir = tmp_path / "busfile"
    not_a_dir.write_text("x", encoding="utf-8")        # bus_dir 指向普通文件 -> makedirs 失败
    monkeypatch.setenv("PRISM_BUS_DIR", str(not_a_dir))
    monkeypatch.setattr(bus, "BusClient",
                        lambda b, timeout=None: _Fake(_OK_PLAN({"groups": []})))
    env = server.analyze_instancing(export=True)
    assert env["ok"] is True                          # 方案本身取到了
    exp = env["result"]["export"]
    assert exp["ok"] is False and exp["error"]


def test_domain_grouping_is_shared_with_compose_rule():
    # 合批口径单点：actors.py 的探针被两处复用，不能各算各的
    from prism.domain import actors
    assert hasattr(actors, "_mesh_probe") and hasattr(opt_instancing, "_group_rows")
    out = opt_instancing._group_rows([("/Game/M/A", "StaticMeshActor", "P", "A_%d" % i)
                                      for i in range(7)], min_count=5, include_samples=2)
    assert out["groups"][0]["actors"] == 7 and out["collapsible_actors"] == 6

