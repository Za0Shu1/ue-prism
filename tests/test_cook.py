"""PR-2 cook/package 测试：假 UAT 引擎靶子（无真引擎、跨平台）。

覆盖：dry_run 无副作用 / 双钥确认 / cook 端到端成败 / BUSY 互斥 /
跨重启日志收束行合成 done / package 命令模板 / 平台与环境门禁。
"""
from __future__ import annotations

import json
import os
import sys
import time

import pytest

from prism import envelope, server, tasks, uat

FAKE_UAT = (
    "import os, sys, time\n"
    "mode = os.environ.get('FAKE_UAT_MODE', 'ok')\n"
    "print('[fake-uat] mode=' + mode)\n"
    "if mode == 'slow':\n"
    "    time.sleep(4)\n"
    "if mode in ('ok', 'slow'):\n"
    "    print('RETURN: # Success')\n"
    "    sys.exit(0)\n"
    "print('RETURN: Error')\n"
    "sys.exit(1)\n"
)


@pytest.fixture
def uat_env(tmp_path, monkeypatch):
    engine = tmp_path / "engine"
    batch = engine / "Engine" / "Build" / "BatchFiles"
    batch.mkdir(parents=True)
    fake = batch / "fake_uat.py"
    fake.write_text(FAKE_UAT, encoding="utf-8")
    if sys.platform == "win32":
        (batch / "RunUAT.bat").write_text(
            "@echo off\r\n\"%s\" \"%s\" %%*\r\n" % (sys.executable, str(fake)), encoding="utf-8")
    else:
        runner = batch / "RunUAT.sh"
        runner.write_text("#!/bin/sh\nexec \"%s\" \"%s\" \"$@\"\n" % (sys.executable, str(fake)),
                          encoding="utf-8")
        os.chmod(str(runner), 0o755)
    project = tmp_path / "Proj"
    project.mkdir()
    (project / "Proj.uproject").write_text(
        json.dumps({"FileVersion": 3, "EngineAssociation": "5.4"}), encoding="utf-8")
    bus = str(tmp_path / "Prism")
    monkeypatch.setenv("PRISM_ENGINE_ROOT", str(engine))
    monkeypatch.setenv("FAKE_UAT_MODE", "ok")
    server._CFG["project_dir"] = str(project)
    server._CFG["bus_dir"] = bus
    try:
        yield {"engine": str(engine), "project": str(project), "bus": bus}
    finally:
        server._CFG["project_dir"] = None
        server._CFG["bus_dir"] = None


def _wait(task_id, wanted, timeout=15.0):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        env = server.get_cook_status(task_id)
        assert env["ok"], env
        last = env["result"]
        if last["status"] == wanted:
            return last
        time.sleep(0.25)
    raise AssertionError("status %r not reached, last=%r" % (wanted, last))


def test_dry_run_default_no_side_effects(uat_env):
    env = server.cook_package()
    assert env["ok"] and env["result"]["dry_run"] is True
    cmd = env["result"]["command"]
    assert "BuildCookRun" in cmd and "-iterate" in cmd
    assert "-cook" in cmd and "-skipstage" in cmd and "-nop4" in cmd  # 真机校准模板
    assert env["result"]["checks"]["engine_root"] == uat_env["engine"]
    assert tasks.list_records(uat_env["bus"]) == []  # 零副作用


def test_single_key_refused(uat_env):
    env = server.cook_package(dry_run=False, confirm=False)
    assert env["ok"] and env["result"]["dry_run"] is True
    assert "confirm=True" in env["result"]["note"]
    assert tasks.list_records(uat_env["bus"]) == []


def test_cook_success_e2e(uat_env):
    env = server.cook_package(dry_run=False, confirm=True)
    assert env["ok"], env
    tid = env["result"]["task_id"]
    done = _wait(tid, tasks.STATUS_SUCCEEDED)
    assert done["detail"]["exit_code"] == 0
    assert "fake-uat" in "\n".join(done["log_tail"])


def test_cook_failure_exit1(uat_env, monkeypatch):
    monkeypatch.setenv("FAKE_UAT_MODE", "fail")
    env = server.cook_package(dry_run=False, confirm=True)
    assert env["ok"], env
    done = _wait(env["result"]["task_id"], tasks.STATUS_FAILED)
    assert done["detail"]["exit_code"] == 1


def test_busy_mutex_during_run(uat_env, monkeypatch):
    monkeypatch.setenv("FAKE_UAT_MODE", "slow")
    first = server.cook_package(dry_run=False, confirm=True)
    assert first["ok"], first
    busy = server.cook_package(dry_run=False, confirm=True)
    assert busy["ok"] is False and busy["error"]["code"] == envelope.Code.BUSY
    assert first["result"]["task_id"] in busy["error"]["message"]
    _wait(first["result"]["task_id"], tasks.STATUS_SUCCEEDED, timeout=20)
    second = server.cook_package(dry_run=False, confirm=True)
    assert second["ok"], second  # 终结后释放


def test_cross_restart_synthesis_from_log(uat_env):
    """server 死过一轮：done 缺失，但日志有 RunUAT 收束行 -> 读时合成结局。"""
    b = uat_env["bus"]
    rec = tasks.create(b, "cook", uat_env["project"], "cmd", os.path.join(b, "orphan.log"))
    with open(rec["log_path"], "w") as f:
        f.write("noise\nRETURN: Error\n")
    rec = tasks.attach_launch(b, rec, os.getpid())  # pid 活着，模拟"看起来在跑"
    v = server.get_cook_status(rec["task_id"])
    assert v["ok"] and v["result"]["status"] == tasks.STATUS_FAILED
    assert v["result"]["detail"]["exit_code"] == 1


def test_package_command_template(uat_env):
    env = server.cook_package(mode="package", output_dir=r"D:\out")
    cmd = env["result"]["command"]
    assert "BuildCookRun" in cmd and "-stage" in cmd and "-pak" in cmd and "-archive" in cmd
    assert "-fullcook" not in cmd  # package 不走 iterate 分支


def test_platform_guard(uat_env):
    env = server.cook_package(platform="Android")
    assert env["ok"] is False
    assert env["error"]["code"] == envelope.Code.PACKAGE_ENV_MISSING


def test_engine_not_found(monkeypatch, uat_env, tmp_path):
    monkeypatch.delenv("PRISM_ENGINE_ROOT")
    stray = tmp_path / "stray"
    stray.mkdir()
    (stray / "S.uproject").write_text(json.dumps({"EngineAssociation": "99.9-nope"}))
    server._CFG["project_dir"] = str(stray)
    env = server.cook_package()
    assert env["ok"] is False
    assert env["error"]["code"] == envelope.Code.PACKAGE_ENV_MISSING


def test_status_unknown_task(uat_env):
    env = server.get_cook_status("cook-nope-0000")
    assert env["ok"] is False
    assert env["error"]["code"] == envelope.Code.TASK_NOT_FOUND


# ---------- MCP 字符串 bool 归一化（truthy 陷阱回归） ----------

def test_string_bool_double_key_launches(uat_env):
    """MCP 通道把 bool 以字符串送达："false"/"true" 必须能真正满足双钥启动（回归 "False" 恒真陷阱）。"""
    env = server.cook_package(dry_run="false", confirm="true")
    assert env["ok"], env
    assert env["result"].get("task_id")
    _wait(env["result"]["task_id"], "succeeded")


def test_string_single_key_refused(uat_env):
    env = server.cook_package(dry_run="false", confirm="false")
    assert env["ok"] and env["result"]["dry_run"] is True
    assert tasks.list_records(uat_env["bus"]) == []   # 单钥仍零副作用


def test_unparsable_bool_rejected(uat_env):
    env = server.cook_package(dry_run="maybe", confirm="true")
    assert env["ok"] is False and env["error"]["code"] == envelope.Code.RUNTIME_ERROR
    assert tasks.list_records(uat_env["bus"]) == []
