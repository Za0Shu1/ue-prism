"""通用总线调用：向编辑器 bridge 发任意领域函数（真机手测走 bridge 的工具）。

用法:
    python scripts/bus_call.py <bus_dir> <fn> [key=value ...]
    python scripts/bus_call.py <bus_dir> <fn> '{"json": "args"}'

例（cmd / PowerShell 都可）:
    python scripts/bus_call.py "<proj>/Saved/Prism" ping
    python scripts/bus_call.py "<proj>/Saved/Prism" list_level_actors limit=30
    python scripts/bus_call.py "<proj>/Saved/Prism" list_level_actors class_contains=Skeletal limit=50

值自动转换：true/false/none、整数、浮点，否则按字符串。超时读 PRISM_TIMEOUT（默认 30s）。
打印统一信封 JSON；ok=0 退出码否则 1，参数错误=2。
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from prism import bus  # noqa: E402


def _coerce(v):
    low = v.lower()
    if low in ("true", "false"):
        return low == "true"
    if low in ("none", "null"):
        return None
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v)
    except ValueError:
        pass
    return v


def parse_args(tokens):
    if not tokens:
        return {}
    if len(tokens) == 1 and tokens[0].lstrip()[:1] in ("{", "["):
        return json.loads(tokens[0])
    out = {}
    for tok in tokens:
        if "=" in tok:
            key, val = tok.split("=", 1)
            out[key] = _coerce(val)
        else:
            raise ValueError("unrecognized arg %r (use key=value or a single JSON object)" % tok)
    return out


def main():
    a = sys.argv
    if len(a) < 3:
        print(__doc__)
        return 2
    bus_dir, fn = a[1], a[2]
    try:
        args = parse_args(a[3:])
    except Exception as e:
        print("bad args: %s" % e, file=sys.stderr)
        return 2
    timeout = float(os.environ.get("PRISM_TIMEOUT", "30"))
    env = bus.BusClient(bus_dir, timeout=timeout).call(fn, args)
    print(json.dumps(env, ensure_ascii=False, indent=2))
    return 0 if env.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())