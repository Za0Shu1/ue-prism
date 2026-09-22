"""PR-0.5 契约测试：信封透传 / 心跳快速失败 / 陈旧限流 / 孤儿清扫。纯标准库。"""
from __future__ import annotations

import json
import os
import threading
import time

from prism import bridge, bus, envelope
from prism.domain import FUNCTIONS


def test_handler_passes_through_envelope():
    """领域函数自返错误信封：必须透传，绝不二次包裹成 ok。"""
    def _fn():
        return envelope.make_err(envelope.Code.UE_API_MISMATCH, "boom")
    FUNCTIONS["_t_env_err"] = _fn
    try:
        env = bridge.handler("_t_env_err", {})
        assert env["ok"] is False
        assert env["error"]["code"] == envelope.Code.UE_API_MISMATCH
    finally:
        del FUNCTIONS["_t_env_err"]


def test_handler_wraps_plain_result():
    """裸 dict 结果照旧包裹成 ok 信封。"""
    FUNCTIONS["_t_plain"] = lambda: {"x": 1}
    try:
        env = bridge.handler("_t_plain", {})
        assert env["ok"] is True and env["result"] == {"x": 1}
    finally:
        del FUNCTIONS["_t_plain"]


def test_heartbeat_stale_fastfail(tmp_path):
    bdir = str(tmp_path / "bus")
    os.makedirs(bdir)
    with open(os.path.join(bdir, bus.HEARTBEAT_NAME), "w") as f:
        json.dump({"ts": time.time() - 999.0}, f)
    t0 = time.time()
    env = bus.BusClient(bdir, timeout=5).call("ping", {})
    assert time.time() - t0 < 2.0  # 没等满 timeout
    assert env["ok"] is False
    assert env["error"]["code"] == envelope.Code.BRIDGE_UNREACHABLE
    assert "stale" in env["error"]["message"]


def test_heartbeat_fresh_falls_back_to_timeout(tmp_path):
    """心跳新鲜但没人应答（主线程停摆刚恢复前）：退回常规 BRIDGE_TIMEOUT 路径。"""
    bdir = str(tmp_path / "bus")
    os.makedirs(bdir)
    bus.write_heartbeat(bdir)
    bus.write_heartbeat(bdir)
    env = bus.BusClient(bdir, timeout=0.5).call("ping", {})
    assert env["ok"] is False
    assert env["error"]["code"] == envelope.Code.BRIDGE_TIMEOUT


def test_no_heartbeat_proceeds(tmp_path):
    """旧版 bridge 无心跳文件：预检必须放行（兼容），走超时路径。"""
    bdir = str(tmp_path / "bus")
    env = bus.BusClient(bdir, timeout=0.5).call("ping", {})
    assert env["error"]["code"] == envelope.Code.BRIDGE_TIMEOUT


def test_run_forever_writes_heartbeat(tmp_path):
    bdir = str(tmp_path / "bus")
    threading.Thread(target=bridge.run_forever, args=(bdir, 0.02), daemon=True).start()
    deadline = time.time() + 3.0
    while time.time() < deadline:
        age = bus.heartbeat_age(bdir)
        if age is not None and age < 5.0:
            return
        time.sleep(0.05)
    raise AssertionError("heartbeat not written by run_forever")


def test_sweep_stale_removes_orphans(tmp_path):
    bdir = str(tmp_path / "bus")
    os.makedirs(bdir)
    old_cmd = os.path.join(bdir, "cmd_deadbeef.json")
    old_res = os.path.join(bdir, "res_deadbeef.json")
    fresh_res = os.path.join(bdir, "res_fresh.json")
    orphan_tmp = os.path.join(bdir, "res_tmpleft.json.tmp")
    for p in (old_cmd, old_res, fresh_res, orphan_tmp):
        with open(p, "w") as f:
            f.write("{}")
    past = time.time() - 3600.0
    for p in (old_cmd, old_res, orphan_tmp):
        os.utime(p, (past, past))
    bus.sweep_stale(bdir, 60.0)
    assert not os.path.exists(old_cmd)
    assert not os.path.exists(old_res)
    assert not os.path.exists(orphan_tmp)
    assert os.path.exists(fresh_res)  # 新文件不动
