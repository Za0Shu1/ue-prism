"""domain 层白名单：只暴露注册在 FUNCTIONS 中的只读函数。

硬性约束（见 docs/REQUIREMENTS.md §2）：
- 只用 `unreal` + 标准库；禁止 import 任何 MCP/网络/server 相关库；
- 守 Python 3.7 语法子集（UE 内嵌 Python 3.7->3.11 不等）；
- 写操作函数命名 migrate_/fix_ 且默认 dry_run（v1.0 起才出现）。
"""
from __future__ import annotations

FUNCTIONS = {}


def register(fn):
    FUNCTIONS[fn.__name__] = fn
    return fn


from . import ping  # noqa: E402,F401
from . import actors  # noqa: E402,F401
from . import assets
from . import metrics  # noqa: E402,F401
from . import migrate  # noqa: E402,F401  # v1.0 PR-4：写操作执行器（migrate_*）