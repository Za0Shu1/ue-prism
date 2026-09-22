"""统一返回信封与错误码。纯标准库，server / bridge / domain 三层通用。

契约见 docs/REQUIREMENTS.md §3：错误绝不伪装成成功。
"""
from __future__ import annotations


class Code(object):
    BRIDGE_TIMEOUT = "BRIDGE_TIMEOUT"
    BRIDGE_UNREACHABLE = "BRIDGE_UNREACHABLE"
    UNKNOWN_FN = "UNKNOWN_FN"
    UE_API_MISMATCH = "UE_API_MISMATCH"
    RUNTIME_ERROR = "RUNTIME_ERROR"
    BUSY = "BUSY"  # v0.2 PR-1 预置：任务登记表 BUSY 互斥（prism/tasks.py）
    TASK_NOT_FOUND = "TASK_NOT_FOUND"  # v0.2 PR-1 预置：task_id 不存在
    PACKAGE_ENV_MISSING = "PACKAGE_ENV_MISSING"  # v0.2 PR-2：引擎/UAT/平台环境不满足（附排查提示）
    # v1.0 PR-2：连接模型发现（见 DESIGN_v1.0 §3）
    NO_PROJECT = "NO_PROJECT"  # 无注册/活跃工程（编辑器未开或插件未启）
    AMBIGUOUS_PROJECT = "AMBIGUOUS_PROJECT"  # 多工程并存，需 project= 选择器
    PROJECT_NOT_FOUND = "PROJECT_NOT_FOUND"  # project= 未命中任何已注册工程


def make_ok(result):
    return {"ok": True, "result": result, "error": None}


def make_err(code, message, tb=None):
    return {"ok": False, "result": None, "error": {"code": code, "message": message, "traceback": tb}}