"""v0.2 server 侧任务登记表（docs/DESIGN_v0.2.md §2 的实现，PR-1）。

长任务（cook/package）远超 30s 总线超时 → 落盘跟踪。关键契约：
**没有守护进程，也没有后台轮询** —— server 是被 agent spawn 的短命进程；
任务状态一律在读取时刻由盘上事实惰性推导：

  1. 有 done 标记            -> succeeded / failed（含 exit_code）
  2. 超过硬时限 deadline_ts   -> timeout（即使 pid 显示存活）
  3. 登记 pid 仍存活         -> running
  4. pending 且从未挂接进程   -> failed（"never launched"）
  5. running 但 pid 已消失   -> failed（"abandoned"：launcher 未写退出码就死了）

已知局限：Windows PID 复用可能把「已死」误判为「存活」，由硬时限封顶兜底。

目录布局：<bus_dir>/tasks/<task_id>.json（登记表）与 <task_id>.done.json（退出标记）。
BUSY 互斥：同一 project_dir 同时只允许 1 个未终结任务（UE 锁定 Saved 目录）。
纯标准库；原子写复用 bus 的 tmp+os.replace 约定。
"""
from __future__ import annotations

import glob
import json
import os
import subprocess
import sys
import time
import uuid

from . import bus, envelope

SCHEMA = 1
KINDS = ("cook", "package")
DONE_SUFFIX = ".done.json"
DEFAULT_HARD_LIMIT_S = float(os.environ.get("PRISM_TASK_HARD_LIMIT", str(4 * 3600)))
PENDING_GRACE_S = 60.0  # create 与 attach_launch 之间的合法窗口

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
STATUS_TIMEOUT = "timeout"


class TaskError(Exception):
    """带信封错误码的任务异常；server 工具层负责转成 make_err。"""

    def __init__(self, code, message, task_id=None):
        Exception.__init__(self, message)
        self.code = code
        self.message = message
        self.task_id = task_id


def _tasks_dir(bus_dir):
    return os.path.join(bus_dir, "tasks")


def _rec_path(bus_dir, task_id):
    return os.path.join(_tasks_dir(bus_dir), task_id + ".json")


def _done_path(bus_dir, task_id):
    return os.path.join(_tasks_dir(bus_dir), task_id + DONE_SUFFIX)


def new_task_id(kind):
    return "%s-%s-%s" % (kind, time.strftime("%Y%m%dT%H%M", time.gmtime()), uuid.uuid4().hex[:4])


def _write(bus_dir, path, obj):
    os.makedirs(_tasks_dir(bus_dir), exist_ok=True)
    bus._atomic_write_json(path, obj)


def create(bus_dir, kind, project_dir, command, log_path, extra=None):
    """登记新任务。同工程存在未终结任务 -> TaskError(BUSY)（携带占用者 task_id）。"""
    if kind not in KINDS:
        raise TaskError(envelope.Code.RUNTIME_ERROR, "unknown task kind: %r" % (kind,))
    busy = find_active(bus_dir, project_dir)
    if busy is not None:
        raise TaskError(
            envelope.Code.BUSY,
            "project already has task %s (%s) in status %s" % (
                busy["task_id"], busy["kind"], derive_state(bus_dir, busy)[0]),
            task_id=busy["task_id"],
        )
    now = time.time()
    rec = {
        "schema": SCHEMA,
        "task_id": new_task_id(kind),
        "kind": kind,
        "project_dir": os.path.abspath(project_dir),
        "command": command,
        "log_path": log_path,
        "created_ts": now,
        "started_ts": None,
        "pid": None,
        "deadline_ts": now + DEFAULT_HARD_LIMIT_S,
        "status": STATUS_PENDING,  # 登记态；对外状态看 derive_state
        "note": "",
    }
    if extra:
        rec.update(extra)
    _write(bus_dir, _rec_path(bus_dir, rec["task_id"]), rec)
    return rec


def attach_launch(bus_dir, rec, pid):
    """Popen 成功后立即挂接 pid（状态推导的事实来源）。"""
    rec = dict(rec)
    rec["started_ts"] = time.time()
    rec["pid"] = int(pid)
    rec["status"] = STATUS_RUNNING
    _write(bus_dir, _rec_path(bus_dir, rec["task_id"]), rec)
    return rec


def mark_done(bus_dir, task_id, exit_code):
    _write(bus_dir, _done_path(bus_dir, task_id),
           {"task_id": task_id, "exit_code": int(exit_code), "finished_ts": time.time()})


def load(bus_dir, task_id):
    """读取登记记录；不存在 -> TaskError(TASK_NOT_FOUND)。"""
    try:
        with open(_rec_path(bus_dir, task_id), "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        raise TaskError(envelope.Code.TASK_NOT_FOUND, "no such task: %s" % (task_id,), task_id=task_id)


def _read_done(bus_dir, task_id):
    try:
        with open(_done_path(bus_dir, task_id), "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _pid_alive(pid):
    """跨平台尽力判定。True/False 确定；None = 无法判定（保守视作可能存活）。"""
    if not pid:
        return None
    if sys.platform == "win32":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
            if not handle:
                return False
            try:
                code = ctypes.c_ulong()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return None
                return code.value == 259  # STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return None
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None


def derive_state(bus_dir, rec, now=None):
    """惰性状态推导：(status, detail)。读侧唯一应展示的状态来源。"""
    now = time.time() if now is None else now
    done = _read_done(bus_dir, rec["task_id"])
    if done is not None:
        code = done.get("exit_code")
        status = STATUS_SUCCEEDED if code == 0 else STATUS_FAILED
        return status, {"exit_code": code, "finished_ts": done.get("finished_ts")}
    if now > rec.get("deadline_ts", now + 1):
        return STATUS_TIMEOUT, {"detail": "exceeded hard limit %ss" % DEFAULT_HARD_LIMIT_S}
    if rec.get("status") == STATUS_RUNNING:
        alive = _pid_alive(rec.get("pid"))
        if alive is True:
            return STATUS_RUNNING, {}
        if alive is None:
            return STATUS_RUNNING, {"detail": "pid liveness undetermined"}
        return STATUS_FAILED, {"detail": "abandoned: launcher pid %s gone without exit marker" % rec.get("pid")}
    # pending：attach 竞态窗口外仍未挂接 -> launcher 死在 Popen 前
    if now - rec.get("created_ts", now) > PENDING_GRACE_S:
        return STATUS_FAILED, {"detail": "never launched (stuck pending past grace)"}
    return STATUS_PENDING, {}


def list_records(bus_dir):
    out = []
    for p in sorted(glob.glob(os.path.join(_tasks_dir(bus_dir), "*.json"))):
        if p.endswith(DONE_SUFFIX):
            continue
        try:
            with open(p, "r", encoding="utf-8") as f:
                out.append(json.load(f))
        except (OSError, ValueError):
            continue
    return out


_UNFINISHED = (STATUS_PENDING, STATUS_RUNNING)


def find_active(bus_dir, project_dir):
    """同工程未终结任务（BUSY 互斥的判定依据）。"""
    project_dir = os.path.abspath(project_dir)
    for rec in list_records(bus_dir):
        status, _ = derive_state(bus_dir, rec)
        if status in _UNFINISHED and os.path.abspath(rec.get("project_dir", "")) == project_dir:
            return rec
    return None


def view(bus_dir, task_id):
    """给 get_cook_status 用的聚合视图：登记 + 推导状态 + 日志侧写。"""
    rec = load(bus_dir, task_id)
    status, detail = derive_state(bus_dir, rec)
    log = {"path": rec.get("log_path")}
    try:
        log["size_bytes"] = os.path.getsize(rec["log_path"])
        log["mtime_ts"] = os.path.getmtime(rec["log_path"])
    except (OSError, TypeError):
        log["exists"] = False
    else:
        log["exists"] = True
    return {
        "task_id": rec["task_id"],
        "kind": rec["kind"],
        "status": status,
        "detail": detail or None,
        "created_ts": rec.get("created_ts"),
        "started_ts": rec.get("started_ts"),
        "deadline_ts": rec.get("deadline_ts"),
        "command": rec.get("command"),
        "log": log,
    }


def sweep_finished(bus_dir, older_than_s=30 * 86400.0):
    """清理终结超过时限的任务档案（登记+done+日志），防 tasks/ 膨胀。返回删除的 task_id。"""
    removed = []
    cutoff = time.time() - older_than_s
    for rec in list_records(bus_dir):
        status, _ = derive_state(bus_dir, rec)
        if status in _UNFINISHED:
            continue
        try:
            done = _read_done(bus_dir, rec["task_id"])
            ref_ts = (done or {}).get("finished_ts") or rec.get("created_ts", 0)
        except OSError:
            continue
        if ref_ts < cutoff:
            for p in (_rec_path(bus_dir, rec["task_id"]), _done_path(bus_dir, rec["task_id"])):
                try:
                    os.remove(p)
                except OSError:
                    pass
            log = rec.get("log_path")
            if log:
                try:
                    os.remove(log)
                except OSError:
                    pass
            removed.append(rec["task_id"])
    return removed
