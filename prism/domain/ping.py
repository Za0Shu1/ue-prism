"""ping：bridge 心跳 + 引擎/工程/地图信息。MVP 首发唯一必需工具。

unreal API 名跨 5.0-5.8 有差异 -> 每个字段“多候选兜底”，取到即用、取不到不崩。
project_dir：unreal.Paths 给的是相对引擎根的路径，统一转成绝对；bridge 另注入
PRISM_PROJECT_DIR（从 bus_dir 反推）作兜底。
"""
from __future__ import annotations

import os

from . import register


def _absolutize(unreal, path):
    try:
        return unreal.Paths.convert_relative_path_to_full(path)
    except Exception:
        try:
            return os.path.abspath(path)
        except Exception:
            return path


@register
def ping():
    result = {
        "bridge_alive": True,
        "ue_version": None,
        "project_dir": os.environ.get("PRISM_PROJECT_DIR"),
        "loaded_maps": [],
    }
    try:
        import unreal  # 惰性：无引擎（总线测试）环境走降级返回
    except Exception:
        return result

    # engine version：5.4 首选 SystemLibrary.get_engine_version()
    for getter in (
        lambda: unreal.SystemLibrary.get_engine_version(),
        lambda: unreal.get_engine_version(),
    ):
        try:
            value = getter()
        except Exception:
            continue
        if value:
            result["ue_version"] = value
            break

    # project dir：试 unreal.Paths 并转绝对；失败则保留 env 兜底
    for getter in (
        lambda: unreal.Paths.get_project_dir(),
        lambda: unreal.Paths.project_dir(),
    ):
        try:
            value = getter()
        except Exception:
            continue
        if value:
            result["project_dir"] = _absolutize(unreal, value)
            break

    # loaded maps（world 名）
    try:
        world = unreal.EditorLevelLibrary.get_editor_world()
        if world is not None:
            result["loaded_maps"] = [world.get_name()]
    except Exception:
        pass

    return result