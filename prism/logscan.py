"""read_editor_log 的离线解析：纯标准库，不依赖 UE/bridge（REQUIREMENTS §2 S1）。

UE 编辑器日志（<proj>/Saved/Logs/*.log）行形如：
    [2024.05.01-03.35.12:345][  12]LogStreaming: Error: Failed to load resource 101
本模块把 tail 行按"类别 + 归一化指纹"聚合，返回 count/first_seen/last_seen/sample + 资产归因，供 agent 定位。
指纹归一（P2）：绝对/引擎/相对路径、带引号串、十六进制地址、数字 -> 占位符，让"同模板不同资产/
帧号"的噪音行合并成一个组；同时从原文提取 /Game、/Engine、.uasset/.umap 引用登记到组的 assets。
会话切分（P2）：日志未轮转时同一文件里会有多个 "Log file open" 起始标记（多次启动混在一个 tail），
按该标记把 tail 切成若干会话段，给每条指纹标注落在哪些会话、是否"本会话新增"，并给每会话的
error/warning/display 计数——供回归对比（新增项即本会话引入）。
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
# 会话起始：UE 每次启动写 "Log file open, <localtime>"（在设日志格式前写，通常无 [ts] 前缀）。
_SESSION_OPEN_RE = re.compile(r"log file open", re.IGNORECASE)

# 指纹归一：路径 / 引擎包 / 相对 -> <path>；停止集只用空白（避开正则里嵌入引号的转义）
_PATH_RE = re.compile(
    r"[A-Za-z]:[\\/][^\s]+|(?:/Game/|/Engine/|/Plugins?/|/Content/|/Script/|\.\./)[^\s]+",
    re.IGNORECASE,
)
_HEX_RE = re.compile(r"0x[0-9a-fA-F]+")
_NUM_RE = re.compile(r"\d+")

# 资产归因：抽 /Game /Engine /Plugins 包路径（可含非 ASCII）与绝对 .uasset/.umap。
# 边界要求引用前是行首/空白/括号/逗号/等号，避免相对路径里的 /Engine 子串被误登记。
_ASSET_RE = re.compile(
    r"(?:^|(?<=[\s(,=]))(?:/Game/|/Engine/|/Plugins?/)[^\s]+|"
    r"[A-Za-z]:[\\/][^\s]*\.(?:uasset|umap)",
    re.IGNORECASE,
)

ASSET_CAP = 20
_LVLS = ("error", "warning", "display")


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
    m = _PATH_RE.sub("<path>", msg)
    m = _HEX_RE.sub("<addr>", m)
    m = _NUM_RE.sub("#", m)
    return " ".join(m.split())


def _extract_assets(msg):
    seen = []
    for tok in _ASSET_RE.findall(msg):
        t = tok.rstrip(".,;:")
        if t and t not in seen:
            seen.append(t)
    return seen


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

    sessions = []
    groups = {}
    total = 0

    def new_session(opened, partial):
        sid = len(sessions)
        sessions.append({
            "session_id": sid, "opened": opened, "partial": partial,
            "first_ts": None, "last_ts": None, "lines": 0,
            "error": 0, "warning": 0, "display": 0,
        })
        return sid

    # 起始先开一段"前置会话"（其 Log file open 多半在 tail 之前，故 partial=True）；
    # 若命中真正的起始标记会另开完整段，空的前置段最后会被剔除。
    cur_sid = new_session(None, True)

    for line in lines:
        if _SESSION_OPEN_RE.search(line):
            cur_sid = new_session(line[:160].strip(), False)
        cur = sessions[cur_sid]
        cur["lines"] += 1
        lm = _LINE.match(line)
        ts = lm.group("ts") if lm else None
        if ts:
            if cur["first_ts"] is None:
                cur["first_ts"] = ts
            cur["last_ts"] = ts
        rest = lm.group("rest") if lm else line
        hm = _HEAD.match(rest)
        cat = hm.group("cat") if hm else "?"
        body = hm.group("body") if hm else rest
        lvl, core = _classify(body)
        lk = lvl.lower()
        if lk in _LVLS:
            cur[lk] += 1
        if want and lvl != want:
            continue
        total += 1
        fp = _norm(core)
        key = cat + "|" + fp
        g = groups.get(key)
        if g is None:
            g = {"category": cat, "level": lvl, "message": core[:400], "fingerprint": fp,
                 "count": 0, "first_seen": ts, "last_seen": ts, "sample": line[:800],
                 "_aset": [], "_sess": set()}
            groups[key] = g
        g["count"] += 1
        if ts:
            if g["first_seen"] is None:
                g["first_seen"] = ts
            g["last_seen"] = ts
        g["_sess"].add(cur_sid)
        for a in _extract_assets(core):
            if a not in g["_aset"]:
                g["_aset"].append(a)

    nonempty = [s for s in sessions if s["lines"] > 0]
    latest_sid = nonempty[-1]["session_id"] if nonempty else None

    ordered = []
    for g in groups.values():
        aset = g.pop("_aset")
        sset = g.pop("_sess")
        g["assets"] = aset[:ASSET_CAP]
        g["asset_count"] = len(aset)
        g["assets_truncated"] = len(aset) > ASSET_CAP
        g["sessions"] = sorted(sset)
        g["session_span"] = len(sset)
        g["in_latest_session"] = (latest_sid is not None and latest_sid in sset)
        g["new_in_latest_session"] = (latest_sid is not None and sset == {latest_sid})
        ordered.append(g)
    ordered.sort(key=lambda x: x["count"], reverse=True)

    note = None
    if len(nonempty) > 1:
        note = ("尾部 %d 行含 %d 个编辑器会话(日志未轮转/多次启动)；groups 用 sessions/session_span/"
                "new_in_latest_session 标注每条指纹归属会话，回归看 new_in_latest_session=True 即本会话新增。"
                % (len(lines), len(nonempty)))

    return {
        "log_file": path,
        "source": "file",
        "realtime": False,
        "log_mtime": datetime.fromtimestamp(mtime).isoformat(timespec="seconds"),
        "age_seconds": round(time.time() - mtime, 1),
        "level": level,
        "scanned_lines": len(lines),
        "session_count": len(nonempty),
        "multi_session_in_tail": len(nonempty) > 1,
        "sessions": nonempty,
        "total_matched": total,
        "distinct": len(ordered),
        "cap": int(top),
        "truncated": len(ordered) > int(top),
        "note": note,
        "groups": ordered[:int(top)],
    }
