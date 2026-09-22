"""prism 自动启动脚本：放进 UE 工程 Config/DefaultEngine.ini 的 StartupScripts 后，
编辑器一开就在主线程注册 bridge tick，无需再手动进 [PY] 控制台。

用法（工程 DefaultEngine.ini）：
    [/Script/PythonScriptPlugin.PythonScriptPluginSettings]
    +StartupScripts="<本仓库绝对路径>/scripts/autostart_bridge.py"

路径解析：脚本自己知道仓库根（__file__），bus_dir 从 PRISM_UE_PROJECT 环境变量或
同盘默认工程回退。换工程只改那个环境变量/ini，不用动本文件。
"""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

_project = os.environ.get("PRISM_UE_PROJECT")
if not _project:
    try:
        import unreal  # 编辑器环境：直接问引擎当前工程 dir，最准
        _project = unreal.SystemLibrary.get_project_directory()
    except Exception:
        _project = None

if _project:
    bus_dir = os.path.join(str(_project).rstrip("\\/"), "Saved", "Prism")
    import prism.bridge as b
    b.start(bus_dir.replace("\\", "/"))
else:
    import prism  # noqa: F401
    print("[prism] autostart skipped: cannot resolve UE project dir")