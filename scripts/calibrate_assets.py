"""无头校准 describe_asset / get_asset_references 的 AssetRegistry 签名（UE 5.x）。

运行（README §本机真机测试 第 3 步同款）：
    UnrealEditor-Cmd.exe <uproject> -run=pythonscript -script="<本文件绝对路径>"

结果写 <repo>/_calib_out.json（供开发侧读取），并 unreal.log 打印。只读、不改工程。
"""
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

import unreal  # 引擎环境内可用
from prism.domain import assets

PROJECT_DIR = unreal.SystemLibrary.get_project_directory()
# 挑几个一定存在且带引用关系的真实资产做样本
samples = [
    "/Game/UE_PRISM_TestMap",
    "/Game/StarterContent/Textures/T_Rock_Slate_D",
    "/Game/StarterContent/Materials/M_Basic_Wall",
]

out = {"project_dir": str(PROJECT_DIR), "python": sys.version.split()[0], "cases": []}
for sp in samples:
    case = {"asset_path": sp}
    try:
        case["describe"] = assets.describe_asset(sp)
    except Exception as e:
        case["describe"] = {"exception": repr(e)}
    try:
        case["refs"] = assets.get_asset_references(sp, direction="both", recursive=False, limit=25)
    except Exception as e:
        case["refs"] = {"exception": repr(e)}
    out["cases"].append(case)

dst = os.path.join(REPO, "_calib_out.json")
with open(dst, "w", encoding="utf-8") as fh:
    json.dump(out, fh, indent=1, ensure_ascii=False)
unreal.log("[prism-calib] wrote %s" % dst)
print("[prism-calib] " + json.dumps(out, ensure_ascii=False))