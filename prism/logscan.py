"""read_editor_log 的离线解析：纯标准库，不依赖 UE/bridge（docs/REQUIREMENTS.md §2 S1）。

UE 编辑器日志（<proj>/Saved/Logs/*.log）行形如：
    [2024.05.01-03.35.12:345][  12]LogStreaming: Error: Failed to load resource 101
本模块把 tail 行按"类别 + 归一化消息"聚合，返回 count/first_seen/sample，供 agent 归因。
注意：读的是磁盘上的历史文件（realtime=false），编辑器关闭后仍能读上次落盘内容。
"""
from __future__ import annotations

import glob
import os
import re
import time
from collections import deque
from datetime import datetime

_LINE = re.compile(r"^\[(?P<ts>[0-9]{4}\.[0-9]{2}\.[0-9]{2}-[0-9:. ]+)\]\[[ 0-9]*\]\s*(?P<rest>.*)$")
_HEAD = re.compile(r"^(?P<cat>[A-Za-z0-9_]+):\s*(?P<body>.*)$")


def _classify(body):
    b = body.strip()
    low = b.lower()
    for prefix in ("error:", "fatal:"):
        if low.startswith(prefix):
            return "Error", b[len(prefix):].strip()
    if low.startswith("warning:"):
        return "Warning", b[len("warning:"):].strip()
    return "Display", b


def _norm(msg):
    m = re.sub(r"0x[0-9a-fA-F]+", "<addr>", msg)
    m = re.sub(r"\d+", "#", m)
    return " ".join(m.split())


def _find_log(project_dir):
    d = os.path.join(project_dir, "Saved", "Logs")
    files = glob.glob(os.path.join(d, "*.log"))
    if not files:
        raise FileNotFoundError("no *.log under " + d)
    return max(files, key=os.path.getmtime)


def _tail_lines(path, n):
    dq = deque(maxlen=max(1, int(n)))
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            dq.append(line.rstrip("\n"))
    return list(dq)


def read_editor_log(project_dir, level="Error", tail=2000, top=30):
    path = _find_log(project_dir)
    mtime = os.path.getmtime(path)
    lines = _tail_lines(path, tail)
    want = None if str(level).lower() in ("all", "", "none") else str(level).capitalize()
    groups = {}
    total = 0
    for line in lines:
        lm = _LINE.match(line)
        ts = lm.group("ts") if lm else None
        rest = lm.group("rest") if lm else line
        hm = _HEAD.match(rest)
        cat = hm.group("cat") if hm else "?"
        body = hm.group("body") if hm else rest
        lvl, core = _classify(body)
        if want and lvl != want:
            continue
        total += 1
        key = cat + "|" + _norm(core)
        g = groups.get(key)
        if g is None:
            groups[key] = {
                "category": cat, "level": lvl,
                "message": core[:400], "count": 1,
                "first_seen": ts, "sample": line[:800],
            }
        else:
            g["count"] += 1
    ordered = sorted(groups.values(), key=lambda g: g["count"], reverse=True)
    return {
        "log_file": path,
        "source": "file",
        "realtime": False,
        "log_mtime": datetime.fromtimestamp(mtime).isoformat(timespec="seconds"),
        "age_seconds": round(time.time() - mtime, 1),
        "level": level,
        "scanned_lines": len(lines),
        "total_matched": total,
        "distinct": len(ordered),
        "cap": int(top),
        "truncated": len(ordered) > int(top),
        "groups": ordered[:int(top)],
    }