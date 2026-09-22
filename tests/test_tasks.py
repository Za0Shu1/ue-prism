"""PR-1 任务登记表测试：惰性状态推导 / BUSY 互斥 / 视图聚合 / 清扫（无引擎）。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import pytest

from prism import envelope, tasks


def _mk(bus_dir, kind="cook", project="P"):
    proj = os.path.join(str(bus_dir), project)
    log = os.path.join(str(bus_dir), project + ".log")
    return tasks.create(bus_dir, kind, proj, "fake-uat cmd", log)


def _rewrite(bus_dir, rec):
    tasks._write(bus_dir, tasks._rec_path(bus_dir, rec["task_id"]), rec)


def test_create_load_roundtrip(tmp_path):
    b = str(tmp_path)
    rec = _mk(b)
    assert rec["schema"] == tasks.SCHEMA
    assert rec["task_id"].startswith("cook-")
    loaded = tasks.load(b, rec["task_id"])
    assert loaded == rec
    # 原子写不留 tmp
    assert not [f for f in os.listdir(os.path.join(b, "tasks")) if f.endswith(".tmp")]


def test_unknown_kind_rejected(tmp_path):
    with pytest.raises(tasks.TaskError) as e:
        _mk(str(tmp_path), kind="deploy")
    assert e.value.code == envelope.Code.RUNTIME_ERROR


def test_busy_mutex_and_release(tmp_path):
    b = str(tmp_path)
    first = _mk(b)
    with pytest.raises(tasks.TaskError) as e:
        _mk(b, kind="package")  # 互斥按工程，跨 kind 同样挡
    assert e.value.code == envelope.Code.BUSY
    assert e.value.task_id == first["task_id"]
    # 别的工程不受牵连
    other = _mk(b, project="Q")
    assert other["task_id"] != first["task_id"]
    # 终结后释放
    tasks.mark_done(b, first["task_id"], 0)
    again = _mk(b)
    assert again["task_id"] != first["task_id"]


def test_derive_running_then_succeeded(tmp_path):
    b = str(tmp_path)
    rec = tasks.attach_launch(b, _mk(b), os.getpid())
    status, _ = tasks.derive_state(b, rec)
    assert status == tasks.STATUS_RUNNING
    tasks.mark_done(b, rec["task_id"], 0)
    status, detail = tasks.derive_state(b, rec)
    assert status == tasks.STATUS_SUCCEEDED and detail["exit_code"] == 0


def test_derive_failed_exit_code(tmp_path):
    b = str(tmp_path)
    rec = tasks.attach_launch(b, _mk(b), os.getpid())
    tasks.mark_done(b, rec["task_id"], 1)
    status, detail = tasks.derive_state(b, rec)
    assert status == tasks.STATUS_FAILED and detail["exit_code"] == 1


def test_derive_timeout_beats_live_pid(tmp_path):
    """done 标记缺失且超硬时限：即使 pid 活着也报 timeout。"""
    b = str(tmp_path)
    rec = tasks.attach_launch(b, _mk(b), os.getpid())
    rec["deadline_ts"] = time.time() - 1
    _rewrite(b, rec)
    status, detail = tasks.derive_state(b, rec)
    assert status == tasks.STATUS_TIMEOUT
    assert "hard limit" in detail["detail"]


def test_derive_abandoned_dead_pid(tmp_path):
    b = str(tmp_path)
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    rec = tasks.attach_launch(b, _mk(b), proc.pid)
    status, detail = tasks.derive_state(b, rec)
    assert status == tasks.STATUS_FAILED
    assert "abandoned" in detail["detail"]


def test_derive_pending_never_launched(tmp_path):
    b = str(tmp_path)
    rec = _mk(b)
    rec["created_ts"] = time.time() - 3600  # 越过 attach 竞态窗口
    _rewrite(b, rec)
    status, detail = tasks.derive_state(b, rec)
    assert status == tasks.STATUS_FAILED
    assert "never launched" in detail["detail"]


def test_view_aggregates(tmp_path):
    b = str(tmp_path)
    rec = tasks.attach_launch(b, _mk(b), os.getpid())
    v = tasks.view(b, rec["task_id"])
    assert v["status"] == tasks.STATUS_RUNNING
    assert v["log"]["exists"] is False  # 假日志未落盘
    with open(v["log"]["path"], "w") as f:
        f.write("x")
    v = tasks.view(b, rec["task_id"])
    assert v["log"]["exists"] is True and v["log"]["size_bytes"] == 1


def test_view_unknown_task(tmp_path):
    with pytest.raises(tasks.TaskError) as e:
        tasks.view(str(tmp_path), "cook-nope-0000")
    assert e.value.code == envelope.Code.TASK_NOT_FOUND


def test_sweep_finished(tmp_path):
    b = str(tmp_path)
    gone = _mk(b, project="Old")
    gone["created_ts"] = time.time() - 40 * 86400
    gone["deadline_ts"] = gone["created_ts"] + 10  # 确保推导为已终结
    _rewrite(b, gone)
    live = tasks.attach_launch(b, _mk(b, project="New"), os.getpid())
    removed = tasks.sweep_finished(b, older_than_s=30 * 86400)
    assert gone["task_id"] in removed
    assert live["task_id"] not in removed
    assert not os.path.exists(tasks._rec_path(b, gone["task_id"]))
    assert os.path.exists(tasks._rec_path(b, live["task_id"]))


def test_pid_alive_basics():
    assert tasks._pid_alive(os.getpid()) is True
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    assert tasks._pid_alive(proc.pid) is False
    assert tasks._pid_alive(None) is None
