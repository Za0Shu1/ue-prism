"""domain 层白名单：只暴露注册在 FUNCTIONS 中的函数（读为主，写需双钥）。

硬性约束（见 REQUIREMENTS §2）：
- 只用 `unreal` + 标准库；禁止 import 任何 MCP/网络/server 相关库；
- 守 Python 3.7 语法子集（UE 内嵌 Python 3.7->3.11 不等）；
- 写操作函数命名 migrate_（搬移）/ opt_（纯优化写入）/ fix_（纠错），都默认 dry_run，
  落盘需 dry_run=False 且 confirm=True 双钥（v1.0 起出现 migrate_，v1.5 起出现 opt_）。
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
from . import optutil  # noqa: E402,F401  # v1.5：opt_/fix_ 共用骨架（自身不注册函数）
from . import opt_texture  # noqa: E402,F401  # v1.5 PR-1/2：纹理流送池诊断 + 批量 VT/MaxSize/压缩
from . import opt_nanite  # noqa: E402,F401  # v1.5 PR-1/3：Nanite 候选/冲突 + 批量开关
from . import opt_instancing  # noqa: E402,F401  # v1.5 PR-1：HISM 合批分析（只出方案）
from . import opt_hism  # noqa: E402,F401  # v1.5 PR-5：HISM 折叠执行（路线 A，写场景 + 回滚清单）
