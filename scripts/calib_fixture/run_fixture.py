# -*- coding: utf-8 -*-
"""ue-prism 校准夹具一键跑：在任意 UE5 工程/任意引擎版本上重建已知真值并校验。

流程: PNG 源图 -> commandlet 生成贴图资产 -> 注入 StartupScript 开 GUI 编辑器
（TextureCube + CalibMap 1500 平铺 actor）-> 等 done 标记 -> checks 逐项核对 ->
还原 ini。产出矩阵 json（ue_version/tried/计时），跨版本汇总即 API 兼容矩阵。

用法:
  python run_fixture.py --project <工程目录或.uproject> --editor <UnrealEditor.exe>
  可选: --skip-assets(资产已在) --checks-only(编辑器已开) --actors N
        --timeout N(编辑器等待秒,默认300) --matrix <out.json> --close-editor

前置: 工程已装 UEPrism 桥插件(prism plugin-install)；本机跑过 pip install ue-prism
(或仓库内 python -m pip install -e .)以提供 PIL。校验阶段直接 import 仓库 prism。
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SYS_PY = sys.executable


def _find_uproject(project):
    if project.lower().endswith(".uproject") and os.path.isfile(project):
        return project
    hits = glob.glob(os.path.join(project, "*.uproject"))
    if not hits:
        raise SystemExit("找不到 .uproject: " + project)
    return hits[0]


def _env_with_tmp(tmp):
    env = dict(os.environ)
    env["PRISM_FIXTURE_TMP"] = tmp
    return env


def _wait_file(path, timeout):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if os.path.isfile(path):
            return True
        time.sleep(3)
    return False


def _inject_startup(ini_path, script_path):
    bak = ini_path + ".prismcalib.bak"
    with open(ini_path, "r", encoding="utf-8-sig") as f:
        original = f.read()
    with open(bak, "w", encoding="utf-8") as f:
        f.write(original)
    if "gen_level_startup.py" in original:
        return bak
    line = (original.rstrip() + "\n\n[/Script/PythonScriptPlugin.PythonScriptPluginSettings]\n"
            "+StartupScripts=\"" + script_path.replace("\\", "/") + "\"\n")
    with open(ini_path, "w", encoding="utf-8") as f:
        f.write(line)
    return bak


def main(argv=None):
    ap = argparse.ArgumentParser(prog="run_fixture")
    ap.add_argument("--project", required=True)
    ap.add_argument("--editor", help="UnrealEditor.exe 全路径")
    ap.add_argument("--skip-assets", action="store_true")
    ap.add_argument("--checks-only", action="store_true")
    ap.add_argument("--actors", default="1500")
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--matrix")
    ap.add_argument("--close-editor", action="store_true")
    a = ap.parse_args(argv)

    uproject = _find_uproject(a.project)
    proj_dir = os.path.dirname(os.path.abspath(uproject))
    tmp = os.path.join(proj_dir, "Saved", "PrismFixture")
    os.makedirs(tmp, exist_ok=True)
    bus_dir = os.path.join(proj_dir, "Saved", "Prism")
    env = _env_with_tmp(tmp)
    if a.actors:
        env["PRISM_FIXTURE_ACTORS"] = str(a.actors)

    if not a.checks_only:
        if not a.editor:
            raise SystemExit("需要 --editor <UnrealEditor.exe>（或 --checks-only）")
        cmdlet = a.editor.replace(".exe", "-Cmd.exe")
        # 1) 源图（同进程函数调用）
        sys.path.insert(0, HERE)
        from make_pngs import make_pngs
        make_pngs(os.path.join(tmp, "pngs"))
        print("[fixture] pngs ok")
        # 2) commandlet 资产
        if not a.skip_assets:
            rc = subprocess.call([
                cmdlet, uproject, "-run=pythonscript",
                "-script=" + os.path.join(HERE, "gen_assets_cmdlet.py"),
                "-stdout", "-unattended", "-nosplash"], env=env)
            print("[fixture] assets commandlet rc=%s" % rc)
        # 3) GUI 编辑器 + StartupScripts 建 Cube/关卡
        ini = os.path.join(proj_dir, "Config", "DefaultEngine.ini")
        bak = None
        if os.path.isfile(ini):
            bak = _inject_startup(ini, os.path.join(HERE, "gen_level_startup.py"))
        else:
            raise SystemExit("缺 DefaultEngine.ini，无法注入 StartupScript: " + ini)
        done = os.path.join(tmp, "done.json")
        if os.path.isfile(done):
            os.remove(done)
        proc = subprocess.Popen([a.editor, uproject], env=env)
        try:
            ok = _wait_file(done, a.timeout)
        finally:
            if bak and os.path.isfile(bak):
                os.replace(bak, ini)
        if not ok:
            raise SystemExit("超时未见 %s：编辑器启动失败或 Python 插件未启用" % done)
        with open(done, encoding="utf-8") as f:
            print("[fixture] level startup:", json.load(f))

    # 4) 校验（总线直连 + 规则引擎走当前仓库代码）
    env["PRISM_TIMEOUT"] = env.get("PRISM_TIMEOUT", "120")
    argv_checks = [SYS_PY, os.path.join(HERE, "checks.py"), bus_dir, proj_dir]
    matrix_out = a.matrix or os.path.join(tmp, "matrix.json")
    argv_checks += ["--out", matrix_out]
    rc = subprocess.call(argv_checks, env=env)
    if a.close_editor:
        subprocess.call(["powershell", "-NoProfile", "-Command",
                         "Get-Process UnrealEditor -ErrorAction SilentlyContinue"
                         " | ForEach-Object { $_.CloseMainWindow() | Out-Null }"])
    print("[fixture] matrix ->", matrix_out)
    return rc


if __name__ == "__main__":
    sys.exit(main())
