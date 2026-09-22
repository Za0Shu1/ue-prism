"""真机校验脚本：在真实 unreal 环境下逐个尝试候选 API 名，校正 domain/ping.py 里的 TODO。

两种跑法：
  A) UE 编辑器 [PY] 控制台：
       exec(open(r"<本仓库绝对路径>/scripts/probe_unreal.py", encoding="utf-8").read())
  B) 无头 commandlet（不依赖编辑器 UI）：
       UnrealEditor-Cmd.exe <uproject> -run=pythonscript -script="<this file>"
"""
from __future__ import annotations

import os
import sys
import traceback

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # 自定位，跨机器可移植


def _try(label, fn):
    try:
        print("[prism-probe] %-45s -> %r" % (label, fn()))
    except Exception as e:  # noqa: BLE001
        print("[prism-probe] %-45s -> ERR %s" % (label, e))


print("[prism-probe] python:", sys.version.split()[0])
try:
    import unreal
    print("[prism-probe] import unreal OK")
except Exception:
    print("[prism-probe] import unreal FAILED -> 请在 UE 内运行（[PY] 或 pythonscript commandlet）")
    traceback.print_exc()
    raise SystemExit(1)

print("---- engine / version ----")
_try("unreal.get_engine_version()", lambda: unreal.get_engine_version())

print("---- project dir / name ----")
_try("unreal.Paths.get_project_dir()", lambda: unreal.Paths.get_project_dir())
_try("unreal.Paths.project_dir()", lambda: unreal.Paths.project_dir())
_try("unreal.ProjectFile.get_project_absolute_name()", lambda: unreal.ProjectFile.get_project_absolute_name())
_try("unreal.ProjectFile.get_project_name()", lambda: unreal.ProjectFile.get_project_name())

print("---- editor world / map ----")
_try("unreal.EditorLevelLibrary.get_editor_world()", lambda: unreal.EditorLevelLibrary.get_editor_world())
try:
    w = unreal.EditorLevelLibrary.get_editor_world()
    _try("  world.get_name()", lambda: w.get_name() if w is not None else None)
    _try("  world.get_world_type()", lambda: w.get_world_type() if w is not None else None)
except Exception:
    pass
_try("unreal.EditorLevelLibrary.get_world_name()", lambda: unreal.EditorLevelLibrary.get_world_name())

print("---- calling real prism.domain.ping() (needs repo on sys.path) ----")
if REPO not in sys.path:
    sys.path.insert(0, REPO)
try:
    from prism.domain import ping as ping_mod
    import json
    print("[prism-probe] prism.domain.ping() ->", json.dumps(ping_mod.ping(), ensure_ascii=False))
except Exception:
    traceback.print_exc()

print("[prism-probe] done")