"""UE 插件打包：把引擎侧模块组装成免编译的纯 Python 插件 UEPrism。

单一事实源：引擎侧闭包模块从**已安装的 prism 包**拷贝，避免与 pip 版本漂移。
产物形态（项目级）：<Project>/Plugins/UEPrism/
    UEPrism.uplugin                 # Modules 空 -> 无 C++、不编译；依赖 PythonScriptPlugin；默认启用
    Content/Python/init_unreal.py   # PythonScriptPlugin 在插件启用时自动执行 -> 自启 bridge
    Content/Python/prism/...        # 引擎侧模块闭包（bridge/bus/envelope/registry/domain）

写边界与 migrate_* 一致：默认 dry-run，plan() 只回计划；assemble() 才落盘（目标存在需 force）。
本模块只在 server/CLI 侧运行（不 import unreal、不进插件）。
"""
from __future__ import annotations

import json
import os
import shutil

PLUGIN_NAME = "UEPrism"

# 引擎侧模块闭包（相对 prism 包根）。改 bridge/domain 依赖时同步这里。
ENGINE_MODULES = [
    "__init__.py",
    "bus.py",
    "envelope.py",
    "registry.py",
    "bridge.py",
    "autostart.py",
    "domain/__init__.py",
    "domain/ping.py",
    "domain/actors.py",
    "domain/assets.py",
    "domain/metrics.py",
    "domain/migrate.py",
]

# 绝不进插件：MCP 协议层 / 命令行 / 离线分析（留在 pip 侧）。用于校验装配产物。
FORBIDDEN_IN_PLUGIN = [
    "server.py", "cli.py", "rules.py", "logscan.py", "folderscan.py",
    "attributelog.py", "tasks.py", "uat.py", "__main__.py",
]


def _rel(rel):
    return rel.replace("/", os.sep).replace("\\", os.sep)


def prism_root():
    """已安装 prism 包的根目录（引擎侧模块的来源单一事实源）。"""
    import prism
    return os.path.dirname(os.path.abspath(prism.__file__))


def default_version():
    import prism
    return getattr(prism, "__version__", "0.0.0")


def plugin_root_for_project(project_dir):
    return os.path.join(str(project_dir).rstrip("\\/"), "Plugins", PLUGIN_NAME)


def _file_list():
    files = ["%s.uplugin" % PLUGIN_NAME, "Content/Python/init_unreal.py"]
    for rel in ENGINE_MODULES:
        files.append("Content/Python/prism/" + rel)
    return files


def _init_unreal_source():
    return (
        "# UEPrism 自启入口（纯 Python，免编译）。由 PythonScriptPlugin 在插件启用时自动执行。\n"
        "import os\n"
        "import sys\n"
        "\n"
        "_here = os.path.dirname(os.path.abspath(__file__))\n"
        "if _here not in sys.path:\n"
        "    sys.path.insert(0, _here)  # 自带 prism 包优先，避免与其它安装漂移\n"
        "\n"
        "try:\n"
        "    import prism.autostart  # noqa: F401  # 模块级 start() 即挂 bridge 到 slate tick\n"
        "except Exception as _e:  # 绝不因插件自启失败而崩编辑器\n"
        "    try:\n"
        "        import unreal\n"
        "        unreal.log_warning(\"[prism] UEPrism autostart failed: %s\" % _e)\n"
        "    except Exception:\n"
        "        print(\"[prism] UEPrism autostart failed: %s\" % _e)\n"
    )


def render_uplugin(version):
    """返回 .uplugin 文本（合法 JSON）。Modules 空 = 纯 Python 插件，不编译。"""
    doc = {
        "FileVersion": 3,
        "Version": 1,
        "VersionName": str(version),
        "FriendlyName": "UE Prism Bridge",
        "Description": "Zero-compile Python bridge that exposes the editor to the ue-prism MCP server over a file bus.",
        "Category": "Programming",
        "CreatedBy": "Za0Shu1",
        "CreatedByURL": "https://github.com/Za0Shu1/ue-prism",
        "DocsURL": "https://github.com/Za0Shu1/ue-prism/blob/main/docs/SETUP.md",
        "MarketplaceURL": "",
        "SupportURL": "",
        "CanContainContent": True,  # 必须 True：PythonScriptPlugin 只扫已挂载 Content 的插件的 Content/Python/init_unreal.py
        "IsBetaVersion": False,
        "IsExperimentalVersion": False,
        "Installed": False,
        "EnabledByDefault": True,
        "Modules": [],
        "Plugins": [
            {"Name": "PythonScriptPlugin", "Enabled": True},
        ],
    }
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"


def _validate_closure(root):
    missing = [rel for rel in ENGINE_MODULES if not os.path.isfile(os.path.join(root, _rel(rel)))]
    if missing:
        raise FileNotFoundError("prism 包缺少引擎侧模块: %s" % ", ".join(missing))


def plan(plugin_root, version=None):
    """只读计划：会写哪些文件、目标是否已存在（冲突）。绝不改动任何东西。"""
    version = version or default_version()
    return {
        "plugin_root": plugin_root.replace("\\", "/"),
        "version": version,
        "files": _file_list(),
        "exists": os.path.isdir(plugin_root),
        "uplugin_exists": os.path.isfile(os.path.join(plugin_root, "%s.uplugin" % PLUGIN_NAME)),
        "note": ("目标插件目录已存在（install 需 force 覆盖）"
                 if os.path.isdir(plugin_root) else "目标不存在，将新建插件目录树"),
    }


def assemble(plugin_root, version=None, force=False):
    """落盘构建插件树。force=False 且目标已存在时拒绝（不覆盖）。返回摘要。"""
    version = version or default_version()
    root = prism_root()
    _validate_closure(root)

    if os.path.isdir(plugin_root) and not force:
        return {"ok": False, "conflict": True,
                "plugin_root": plugin_root.replace("\\", "/"),
                "note": "目标插件目录已存在，未改动。确认覆盖加 force=True。"}

    py_dir = os.path.join(plugin_root, "Content", "Python")
    pkg_dir = os.path.join(py_dir, "prism")
    os.makedirs(plugin_root, exist_ok=True)
    written = []

    with open(os.path.join(plugin_root, "%s.uplugin" % PLUGIN_NAME), "w", encoding="utf-8") as f:
        f.write(render_uplugin(version))
    written.append("%s.uplugin" % PLUGIN_NAME)

    os.makedirs(py_dir, exist_ok=True)
    with open(os.path.join(py_dir, "init_unreal.py"), "w", encoding="utf-8") as f:
        f.write(_init_unreal_source())
    written.append("Content/Python/init_unreal.py")

    for rel in ENGINE_MODULES:
        src = os.path.join(root, _rel(rel))
        dst = os.path.join(pkg_dir, _rel(rel))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
        written.append("Content/Python/prism/" + rel)

    return {"ok": True, "conflict": False,
            "plugin_root": plugin_root.replace("\\", "/"),
            "version": version, "files_written": len(written), "files": written,
            "note": "插件树已生成。开该工程 -> PythonScriptPlugin 自动执行 init_unreal -> bridge 自启。"}