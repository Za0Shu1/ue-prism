"""手测 scan_folder_assets（离线磁盘扫描，不需编辑器/mcp）。

用法:
    python scripts/scan_cli.py <project_dir> [folder] [sort] [limit]
folder 默认 /Game（整个 Content）；sort=size|path；limit 默认 20。打印统一信封 JSON。
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
        print("usage: scan_cli.py <project_dir> [folder] [sort] [limit]", file=sys.stderr)
        return 2
    folder = a[2] if len(a) > 2 else "/Game"
    sort = a[3] if len(a) > 3 else "size"
    limit = int(a[4]) if len(a) > 4 else 20
    env = server.scan_folder_assets(folder=folder, sort=sort, limit=limit, project_dir=proj)
    print(json.dumps(env, ensure_ascii=False, indent=2))
    return 0 if env.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())