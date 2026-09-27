# -*- coding: utf-8 -*-
"""claim-before-process 语义（2026-09-27 手动真机测抓出的写命令双执行缺陷）。

- 同一 cmd 只会被处理一次（serve_once 重入/多实例不双跑）；
- proc_ 残骸永不被执行，只会被 sweep_stale 回收；
- handler 崩溃不裸崩且 cmd/claim 不残留。
"""
from __future__ import annotations

import json
import os
import time

from prism import bus


def _drop_cmd(bus_dir, cid, fn="ping", args=None):
    p = os.path.join(bus_dir, "cmd_%s.json" % cid)
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"id": cid, "fn": fn, "args": args or {}}, f)
    return p


def test_single_claim_no_double_execution(tmp_path):
    b = str(tmp_path / "bus")
    os.makedirs(b)
    calls = []

    def handler(fn, args):
        calls.append(fn)
        return {"ok": True, "result": {}, "error": None}

    _drop_cmd(b, "x1", fn="do_write")
    assert bus.serve_once(b, handler) is True
    assert bus.serve_once(b, handler) is False  # cmd 已被 claim 消费
    assert calls == ["do_write"]
    assert os.path.isfile(os.path.join(b, "res_x1.json"))


def test_proc_files_never_executed(tmp_path):
    b = str(tmp_path / "bus")
    os.makedirs(b)
    with open(os.path.join(b, "proc_ghost.json"), "w", encoding="utf-8") as f:
        json.dump({"id": "ghost", "fn": "do_write", "args": {}}, f)

    def handler(fn, args):
        raise AssertionError("must not execute proc_ leftovers")

    assert bus.serve_once(b, handler) is False


def test_sweep_reclaims_proc_debris(tmp_path):
    b = str(tmp_path / "bus")
    os.makedirs(b)
    p = os.path.join(b, "proc_old.json")
    with open(p, "w", encoding="utf-8") as f:
        f.write("{}")
    old = time.time() - 9999
    os.utime(p, (old, old))
    bus.sweep_stale(b, 60)
    assert not os.path.exists(p)


def test_handler_crash_returns_err_not_replay(tmp_path):
    b = str(tmp_path / "bus")
    os.makedirs(b)

    def handler(fn, args):
        raise RuntimeError("boom")

    _drop_cmd(b, "c1")
    assert bus.serve_once(b, handler) is True
    env = json.load(open(os.path.join(b, "res_c1.json"), encoding="utf-8"))
    assert env["ok"] is False
    assert not os.path.exists(os.path.join(b, "cmd_c1.json"))
    assert not os.path.exists(os.path.join(b, "proc_c1.json"))
    # 再跑一轮不会重复执行（无残骸）
    assert bus.serve_once(b, handler) is False
