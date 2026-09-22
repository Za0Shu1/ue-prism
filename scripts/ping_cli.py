"""从总线外部发一个 ping 并打印信封。用于不装 MCP 客户端时手测连通。

用法:
    python scripts/ping_cli.py [bus_dir]
不带 bus_dir 时读环境变量 PRISM_BUS_DIR；超时读 PRISM_TIMEOUT（默认 30s）。
退出码：ok=0，否则 1；参数缺失=2。
"""
from __future__ import annotations

import json
import os
import sys

# 允许从任意 cwd 运行：把仓库根加入 sys.path 以 import prism
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from prism import bus  # noqa: E402


def main():
    if len(sys.argv) > 1:
        bus_dir = sys.argv[1]
    else:
        bus_dir = os.environ.get("PRISM_BUS_DIR")
    if not bus_dir:
        print("no bus dir: pass one or set PRISM_BUS_DIR", file=sys.stderr)
        return 2
    timeout = float(os.environ.get("PRISM_TIMEOUT", "30"))
    env = bus.BusClient(bus_dir, timeout=timeout).call("ping", {})
    print(json.dumps(env, ensure_ascii=False, indent=2))
    return 0 if env.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())