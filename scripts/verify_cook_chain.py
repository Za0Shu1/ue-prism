"""L2 真机 cook/package 全链路验证（server 侧；编辑器需关闭）。

补齐 cook_package 的真实全链路空档：dry-run 组装 -> 双钥真执行 -> 轮询状态 ->
校验产物落盘 -> 日志错误归因。UAT 独立起 headless 进程（cook=只 cook 不 stage；
package=-build 编译 + cook + stage + pak + archive），要求编辑器关闭以免资源/
二进制占用，故与 run_fixture 互斥、单独成脚本。cook 归 L2、package 归 L3。

用法：
    python scripts/verify_cook_chain.py --project <工程目录> [--mode cook|package]
                                        [--map /Game/PrismCalib/CalibMap]
                                        [--configuration Development]
                                        [--output-dir <打包归档目录>]
                                        [--timeout 600] [--dry] [--skip-artifact]
  --mode           cook（默认，-cook -skipstage）或 package（-build -cook -stage -pak -archive）。
  --dry            只跑 dry-run 组装校验，不启动真实执行（快速冒烟，无需等待）。
  --map            要 cook 的地图（仅 cook 模式生效；package 走全量构建，忽略之）。
  --configuration  构建配置（Development/Shipping）。
  --output-dir     package 归档目录；缺省用 UE 默认 <project>/Archived。
  --timeout        真实执行轮询上限秒数；package 首编耗时长，建议 >=1800。
全 PASS 退出 0；任一 FAIL 退出 1。真实机器路径只打印在本机 stdout，不入库。
"""
import os
import sys
import time
import glob
import argparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from prism import server, registry, uat  # noqa: E402

DEFAULT_MAP = "/Game/PrismCalib/CalibMap"


class Result:
    def __init__(self, mode="cook"):
        self.mode = mode
        self.passes = 0
        self.fails = 0

    def check(self, name, ok, detail=""):
        if ok:
            self.passes += 1
        else:
            self.fails += 1
        line = "[%s] %s" % ("PASS" if ok else "FAIL", name)
        if detail:
            line += "  -> %s" % (detail,)
        print(line)

    def finish(self):
        print("\n== 汇总 ==  PASS=%d  FAIL=%d" % (self.passes, self.fails))
        if self.fails == 0:
            print("结论： [OK] %s 全链路真机通过" % self.mode)
        else:
            print("结论： [FAIL] 存在未通过项")
        return 0 if self.fails == 0 else 1


def _live_for(project_dir):
    """该工程是否有活跃桥（编辑器开着）。cook/package 都需要它关闭。"""
    want = os.path.abspath(project_dir)
    for e in registry.discover():
        pd = os.path.abspath(e.get("project_dir", ""))
        if pd == want and e.get("live"):
            return e
    return None


def _first_match(root, pattern):
    """归档目录下递归匹配 pattern 的第一个文件（无则 None）。"""
    if not root or not os.path.isdir(root):
        return None
    hits = glob.glob(os.path.join(root, "**", pattern), recursive=True)
    return hits[0] if hits else None


def _cooked_umap(project_dir, map_path):
    """从 cooked 目录找该地图的 .umap。/Game/A/B -> .../Content/A/B.umap（平台目录名兜底）。"""
    rel = map_path[len("/Game"):] if map_path.startswith("/Game") else map_path
    leaf = rel.rpartition("/")[2]
    core = rel.strip("/")
    sub = core.rsplit("/", 1)[0].replace("/", os.sep) if "/" in core else ""
    pat = os.path.join(project_dir, "Saved", "Cooked", "*", "*", "Content", sub, leaf + ".umap")
    hits = glob.glob(pat)
    if not hits:
        hits = glob.glob(os.path.join(project_dir, "Saved", "Cooked", "**", leaf + ".umap"), recursive=True)
    return hits[0] if hits else None


def _dry_run_checks(mode, a, project, r):
    """组装并校验 dry-run 命令；真执行阶段复用同一组参。"""
    d = server.cook_package(platform="Windows", mode=mode, configuration=a.configuration,
                            map=a.map, output_dir=a.output_dir, iterate=None,
                            dry_run=True, confirm=False, project=project)
    dres = d.get("result") or {}
    cmd = dres.get("command", "")
    checks = dres.get("checks") or {}
    how = "runuat=%s engine_how=%s" % (checks.get("runuat_found"), checks.get("engine_root_how"))
    base_ok = (d.get("ok") and dres.get("dry_run") is True and "BuildCookRun" in cmd
               and checks.get("runuat_found") is True and bool(checks.get("engine_root")))
    if mode == "package":
        flags_ok = all(f in cmd for f in ("-build", "-cook", "-stage", "-pak", "-archive")) \
            and "-skipstage" not in cmd
        if a.output_dir:
            flags_ok = flags_ok and ("-archivedirectory" in cmd)
        r.check("package_dry_run_assembles", base_ok and flags_ok, how)
        r.check("package_dry_run_full_build",
                "-iterate" not in cmd and "+maps=" not in cmd,
                "package 走全量构建：无 -iterate/-skipstage，地图由构建期决定")
    else:
        r.check("cook_dry_run_assembles",
                base_ok and "-cook" in cmd and "-skipstage" in cmd, how)
        r.check("cook_dry_run_map_targeted", ('+maps="%s"' % a.map) in cmd, "+maps=%s 在命令里" % a.map)
    return base_ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("--mode", default="cook", choices=["cook", "package"])
    ap.add_argument("--map", default=DEFAULT_MAP)
    ap.add_argument("--configuration", default="Development")
    ap.add_argument("--output-dir", default=None, help="package 归档目录；缺省 <project>/Archived")
    ap.add_argument("--timeout", type=int, default=600)
    ap.add_argument("--dry", action="store_true", help="仅 dry-run 组装，不启动真实执行")
    ap.add_argument("--skip-artifact", action="store_true")
    a = ap.parse_args()
    project = os.path.abspath(a.project)
    mode = a.mode
    r = Result(mode)

    print("== L2 %s 全链路 ==" % mode)
    print("  project=%s" % project)
    print("  mode=%s  config=%s  map=%s  dry_only=%s" % (mode, a.configuration, a.map, a.dry))

    up = uat.find_uproject(project)
    if not up or not os.path.isfile(up):
        r.check("uproject_found", False, "no .uproject under %s" % project)
        return r.finish()
    r.check("uproject_found", True, os.path.basename(up))

    live = _live_for(project)
    if live:
        r.check("editor_offline", False, "桥仍活跃(编辑器开着)——需先关闭编辑器再跑本脚本")
        return r.finish()
    r.check("editor_offline", True, "bridge 非活跃（编辑器已关，符合 %s 前置）" % mode)

    _dry_run_checks(mode, a, project, r)

    if a.dry:
        print("\n(--dry：跳过真实执行)")
        return r.finish()

    s = server.cook_package(platform="Windows", mode=mode, configuration=a.configuration,
                            map=a.map, output_dir=a.output_dir, iterate=None,
                            dry_run=False, confirm=True, project=project)
    if not s.get("ok"):
        r.check("%s_launch" % mode, False, "err=%s" % (s.get("error"),))
        return r.finish()
    task_id = (s.get("result") or {}).get("task_id")
    r.check("%s_launch" % mode, bool(task_id), "task_id=%s" % task_id)

    deadline = time.time() + a.timeout
    final = None
    while time.time() < deadline:
        st = server.get_cook_status(task_id=task_id, tail=3, project=project)
        res = st.get("result") or {}
        if res.get("status") in ("succeeded", "failed", "timeout"):
            final = res
            break
        time.sleep(5)
    exit_code = (final or {}).get("detail", {}).get("exit_code")
    r.check("%s_reaches_done" % mode, final is not None, "status=%s" % (final or {}).get("status"))
    r.check("%s_succeeded" % mode, (final or {}).get("status") == "succeeded", "exit_code=%s" % exit_code)

    if not a.skip_artifact:
        if mode == "package":
            arch_root = os.path.abspath(a.output_dir) if a.output_dir else os.path.join(project, "Archived")
            exe = _first_match(arch_root, "*.exe")
            pak = _first_match(arch_root, "*.pak")
            r.check("package_archive_executable_present", bool(exe),
                    (os.path.relpath(exe, project) if exe else "归档目录未找到可执行 .exe: %s" % arch_root))
            r.check("package_archive_pak_present", bool(pak),
                    (os.path.relpath(pak, project) if pak else "归档目录未找到 .pak: %s" % arch_root))
        else:
            umap = _cooked_umap(project, a.map)
            r.check("cooked_map_present", bool(umap),
                    (os.path.relpath(umap, project) if umap else "Saved/Cooked 下未找到 %s.umap" % a.map))

    att = server.attribute_cook_errors(task_id=task_id, top=10, project=project)
    r.check("attribute_runs", att.get("ok"), "groups=%s" % (att.get("result") or {}).get("total"))

    return r.finish()


if __name__ == "__main__":
    sys.exit(main())