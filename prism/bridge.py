"""bridge：UE 编辑器内的最薄适配器。换引擎/换传输只动这一层。

- 生产：start(bus_dir) 把总线轮询挂到主线程 slate post-tick（UE 5.6+ 禁非 game thread 调 unreal）；
- 测试：run_forever(bus_dir) 阻塞轮询，仅用于无引擎环境（docs/REQUIREMENTS.md §4.1）。
"""
from __future__ import annotations

import traceback

from . import bus, envelope, registry
from .domain import FUNCTIONS

_tick_handles = []
_bus_dirs = []


def _log(msg):
    """编辑器内走 unreal.log（进 Output Log / LogPython）；无引擎回退 print。"""
    try:
        import unreal
        unreal.log(msg)
    except Exception:
        print(msg)


def handler(fn, args):
    """白名单分发。未知名 -> UNKNOWN_FN；领域函数异常 -> RUNTIME_ERROR。绝不裸崩/伪装成功。"""
    if fn not in FUNCTIONS:
        env = envelope.make_err(envelope.Code.UNKNOWN_FN, "unknown fn: %r" % (fn,))
    else:
        try:
            result = FUNCTIONS[fn](**args)
            if isinstance(result, dict) and "ok" in result and "error" in result:
                env = result  # 领域函数自返信封：透传，绝不二次包裹成 ok
            else:
                env = envelope.make_ok(result)
        except Exception as e:
            env = envelope.make_err(envelope.Code.RUNTIME_ERROR, str(e), traceback.format_exc())
    if env["ok"]:
        _log("[prism] handled %s -> ok" % (fn,))
    else:
        _log("[prism] handled %s -> ERR %s: %s" % (fn, env["error"]["code"], env["error"]["message"]))
    return env


def run_forever(bus_dir, poll=0.1):
    """阻塞轮询。仅供无引擎总线测试使用。"""
    bus.run_forever(bus_dir, handler, poll=poll)


def start(bus_dir):
    # 幂等：重复调用（如 StartupScript + 手动 [PY] 各来一次）先注销旧 tick，避免双轮询竞态。
    if _tick_handles:
        try:
            stop()
        except Exception:
            pass

    # 从 bus_dir（<proj>/Saved/Prism）反推工程根：注入 PRISM_PROJECT_DIR 兜底 + 供 registry 记 project_dir
    _proj = None
    try:
        import os as _os
        _proj = _os.path.dirname(_os.path.dirname(_os.path.abspath(bus_dir)))
        _os.environ.setdefault("PRISM_PROJECT_DIR", _proj)
    except Exception:
        pass
    """在 UE 编辑器 [PY] 控制台调用：
        import prism.bridge as b
        b.start(r'D:/<Proj>/Saved/Prism')
    """
    import unreal  # 需要引擎环境；无引擎请用 run_forever（仅测试）

    def _tick(_delta):
        try:
            bus.write_heartbeat(bus_dir)
            bus.serve_once(bus_dir, handler)
        except Exception as e:
            _log("[prism] tick error: %s" % e)

    handle = unreal.register_slate_post_tick_callback(_tick)
    _tick_handles.append(handle)
    _bus_dirs.append(bus_dir)
    _ver = None
    for _getter in (
        lambda: unreal.SystemLibrary.get_engine_version(),
        lambda: unreal.get_engine_version(),
    ):
        try:
            _v = _getter()
        except Exception:
            continue
        if _v:
            _ver = _v
            break
    try:
        registry.register(bus_dir, _proj or registry.project_from_bus(bus_dir), _ver)
    except Exception as e:
        _log("[prism] registry register failed: %s" % e)
    _log("[prism] bridge started on %s" % bus_dir)
    return handle


def stop():
    import unreal

    for handle in _tick_handles:
        try:
            unreal.unregister_slate_post_tick_callback(handle)
        except Exception:
            pass
    _tick_handles[:] = []
    for d in _bus_dirs:
        bus.remove_heartbeat(d)
        try:
            registry.unregister(d)
        except Exception:
            pass
    _bus_dirs[:] = []
    _log("[prism] bridge stopped")