"""pip 安装后的 bridge 自启入口（运行在 UE 编辑器内嵌 Python）。

与仓库里的 scripts/autostart_bridge.py 的区别：本模块**随 ue-prism 包一起安装**，
UE 侧不再需要 sys.path / 仓库路径。启用方式（工程 DefaultEngine.ini）：
    [/Script/PythonScriptPlugin.PythonScriptPluginSettings]
    +StartupScripts="<site-packages 下>/prism/autostart.py"
或在 [PY] 控制台：import prism.autostart

bus_dir 解析：优先环境变量 PRISM_UE_PROJECT，否则问引擎当前工程目录。
守 3.7 子集；无引擎环境下安全降级（导入不崩、仅打印跳过），便于 server 侧/测试复用。
"""
import os


def _project_dir():
    env = os.environ.get("PRISM_UE_PROJECT")
    if env:
        return env
    try:
        import unreal
    except Exception:
        return None
    try:
        return unreal.SystemLibrary.get_project_directory()
    except Exception:
        return None


def resolve_bus_dir(project=None):
    project = project if project is not None else _project_dir()
    if not project:
        return None
    return os.path.join(str(project).rstrip("\\/"), "Saved", "Prism").replace("\\", "/")


def start(bus_dir=None):
    """启动 bridge（幂等性由 prism.bridge 保证）。返回实际 bus_dir，无法解析工程则 None。"""
    if not bus_dir:
        bus_dir = resolve_bus_dir()
        if not bus_dir:
            print("[prism] autostart skipped: cannot resolve UE project dir")
            return None
    import prism.bridge as b
    b.start(bus_dir)
    return bus_dir


start()