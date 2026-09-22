"""文件总线：server 与 UE 内 bridge 之间经文件系统做 RPC（见 docs/REQUIREMENTS.md §2）。

- 零 socket、零第三方依赖，仅标准库；
- 命令 cmd_<id>.json -> 响应 res_<id>.json，均原子写（tmp + os.replace）；
- 唯一 id 使多客户端天然互不串台；
- 超时报 BRIDGE_TIMEOUT，不挂起；
- 活性标记 heartbeat.json（bridge 限频写、只含时间戳，见 REQUIREMENTS §7.2 语义限定）：
  客户端发起调用前预检，心跳陈旧立即 BRIDGE_UNREACHABLE 快速失败，不空等超时；
- 每次调用顺带清扫超时崩溃残留的孤儿 cmd/res/tmp 文件。
"""
from __future__ import annotations

import glob
import json
import os
import time
import uuid

from . import envelope

DEFAULT_TIMEOUT = float(os.environ.get("PRISM_TIMEOUT", "30"))
CMD_PREFIX = "cmd_"
RES_PREFIX = "res_"
_SUFFIX = ".json"


def _atomic_write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, path)


def _silently_remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


HEARTBEAT_NAME = "heartbeat.json"
HB_MIN_INTERVAL = 1.0  # bridge 侧写入限频（秒）
HB_STALE_SECONDS = float(os.environ.get("PRISM_HEARTBEAT_STALE", "15"))
_last_hb = {}  # 按 bus_dir 各自限频（多桥并存时互不抢占）


def write_heartbeat(bus_dir):
    """bridge 主线程每 tick 调用：限频原子写活性标记（仅 ts，不落业务数据）。"""
    now = time.time()
    key = os.path.abspath(bus_dir)
    if now - _last_hb.get(key, 0.0) < HB_MIN_INTERVAL:
        return
    _last_hb[key] = now
    try:
        os.makedirs(bus_dir, exist_ok=True)
        _atomic_write_json(os.path.join(bus_dir, HEARTBEAT_NAME), {"ts": now})
    except OSError:
        pass


def remove_heartbeat(bus_dir):
    """bridge 主动停止时删除标记：让客户端立即从「无心跳」得知离线，而非等陈旧阈值。"""
    _silently_remove(os.path.join(bus_dir, HEARTBEAT_NAME))


def heartbeat_age(bus_dir):
    """心跳距今秒数；None = 无/坏标记（旧版 bridge 或首轮，客户端退回纯超时等待）。"""
    try:
        with open(os.path.join(bus_dir, HEARTBEAT_NAME), "r", encoding="utf-8") as f:
            ts = float(json.load(f).get("ts"))
        return max(0.0, time.time() - ts)
    except (OSError, ValueError, TypeError):
        return None


def sweep_stale(bus_dir, older_than):
    """清扫 mtime 老于 older_than 秒的 cmd/res/tmp 残留（崩溃/超时遗骸），防目录膨胀。"""
    cutoff = time.time() - older_than
    patterns = (CMD_PREFIX + "*" + _SUFFIX, RES_PREFIX + "*" + _SUFFIX, "*.tmp")
    for pat in patterns:
        for p in glob.glob(os.path.join(bus_dir, pat)):
            try:
                if os.path.getmtime(p) < cutoff:
                    _silently_remove(p)
            except OSError:
                pass


class BusClient(object):
    """server 侧：把一个领域函数调用投到总线，取回信封。"""

    def __init__(self, bus_dir, timeout=DEFAULT_TIMEOUT, poll=0.05):
        self.bus_dir = bus_dir
        self.timeout = timeout
        self.poll = poll

    def call(self, fn, args=None):
        args = args or {}
        cid = uuid.uuid4().hex
        os.makedirs(self.bus_dir, exist_ok=True)
        # 预检：心跳陈旧 -> 立即失败（编辑器关了/主线程停摆），不再空等 timeout
        age = heartbeat_age(self.bus_dir)
        if age is not None and age > HB_STALE_SECONDS:
            return envelope.make_err(
                envelope.Code.BRIDGE_UNREACHABLE,
                "bridge heartbeat stale %.0fs (editor offline or main-thread stall)" % age,
            )
        sweep_stale(self.bus_dir, self.timeout + 60.0)
        cmd_path = os.path.join(self.bus_dir, CMD_PREFIX + cid + _SUFFIX)
        res_path = os.path.join(self.bus_dir, RES_PREFIX + cid + _SUFFIX)
        try:
            _atomic_write_json(cmd_path, {"id": cid, "fn": fn, "args": args})
        except OSError as e:
            return envelope.make_err(envelope.Code.BRIDGE_UNREACHABLE, "write cmd failed: %s" % e)

        deadline = time.time() + self.timeout
        while time.time() < deadline:
            if os.path.exists(res_path):
                try:
                    with open(res_path, "r", encoding="utf-8") as f:
                        env = json.load(f)
                except (OSError, ValueError) as e:
                    return envelope.make_err(envelope.Code.RUNTIME_ERROR, "bad res: %s" % e)
                _silently_remove(res_path)
                _silently_remove(cmd_path)
                return env
            time.sleep(self.poll)

        _silently_remove(cmd_path)
        return envelope.make_err(
            envelope.Code.BRIDGE_TIMEOUT,
            "no bridge response within %.1fs" % self.timeout,
        )


def serve_once(bus_dir, handler):
    """处理一个待处理命令；返回是否处理了。供 bridge 每个主线程 tick 调用（每 tick 只处理一条）。"""
    pattern = os.path.join(bus_dir, CMD_PREFIX + "*" + _SUFFIX)
    for cmd_path in sorted(glob.glob(pattern)):
        if cmd_path.endswith(".tmp"):
            continue
        base = os.path.basename(cmd_path)
        cid = base[len(CMD_PREFIX):-len(_SUFFIX)]
        try:
            with open(cmd_path, "r", encoding="utf-8") as f:
                req = json.load(f)
        except (OSError, ValueError):
            continue  # 半截/坏 json：本轮跳过，等下一次
        env = handler(req.get("fn"), req.get("args") or {})
        res_path = os.path.join(bus_dir, RES_PREFIX + cid + _SUFFIX)
        _atomic_write_json(res_path, env)
        _silently_remove(cmd_path)
        return True
    return False


def run_forever(bus_dir, handler, poll=0.1):
    """阻塞轮询循环。仅用于无引擎的总线测试；生产由 bridge 挂主线程 tick。"""
    while True:
        write_heartbeat(bus_dir)
        serve_once(bus_dir, handler)
        time.sleep(poll)