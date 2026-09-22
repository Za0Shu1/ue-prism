"""手测 read_editor_log（离线：不需编辑器在线、不需 mcp）。

用法:
    python scripts/read_log_cli.py <project_dir> [level] [tail] [top]
level 默认 Error，可选 Warning / All。打印统一信封 JSON。
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from prism import server  # noqa: E402


def main():
    a = sys.argv
    proj = a[1] if len(a) > 1 else os.environ.get("PRISM_PROJECT_DIR")
    if not proj:
        print("usage: read_log_cli.py <project_dir> [level] [tail] [top]", file=sys.stderr)
        return 2
    level = a[2] if len(a) > 2 else "Error"
    tail = int(a[3]) if len(a) > 3 else 2000
    top = int(a[4]) if len(a) > 4 else 30
    env = server.read_editor_log(level=level, tail=tail, top=top, project_dir=proj)
    print(json.dumps(env, ensure_ascii=False, indent=2))
    return 0 if env.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())