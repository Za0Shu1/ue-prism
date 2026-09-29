"""L2 真机 cook 全链路验证（server 侧；编辑器需关闭）。

补齐 cook_package 的真实全链路 L2 空档：dry-run 组装 -> 双钥真执行 -> 轮询状态 ->
校验 cooked 产物落盘 -> 日志错误归因。cook 要求编辑器关闭（UAT 独立起 headless cook），
故与 run_fixture 互斥、单独成脚本。

用法：
    python scripts/verify_cook_chain.py --project <工程目录> [--map /Game/PrismCalib/CalibMap]
                                        [--timeout 600] [--dry] [--skip-artifact]
  --dry   只跑 dry-run 组装校验，不启动真实 cook（快速冒烟，无需等待）。
  --map   要 cook 的地图；默认 /Game/PrismCalib/CalibMap（夹具工程已有）。
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
    def __init__(self):
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
        print("结论：", "[OK] cook 全链路真机通过" if self.fails == 0 else "[FAIL] 存在未通过项")
        return 0 if self.fails == 0 else 1


def _live_for(project_dir):
    """该工程是否有活跃桥（编辑器开着）。cook 需要它关闭。"""
    want = os.path.abspath(project_dir)
    for e in registry.discover():
        pd = os.path.abspath(e.get("project_dir", ""))
        if pd == want and e.get("live"):
            return e
    return None


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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("--map", default=DEFAULT_MAP)
    ap.add_argument("--timeout", type=int, default=600)
    ap.add_argument("--dry", action="store_true", help="仅 dry-run 组装，不启动真 cook")
    ap.add_argument("--skip-artifact", action="store_true")
    a = ap.parse_args()
    project = os.path.abspath(a.project)
    r = Result()

    print("== L2 cook 全链路 ==")
    print("  project=%s" % project)
    print("  map=%s  mode=cook  dry_only=%s" % (a.map, a.dry))

    up = uat.find_uproject(project)
    if not up or not os.path.isfile(up):
        r.check("uproject_found", False, "no .uproject under %s" % project)
        return r.finish()
    r.check("uproject_found", True, os.path.basename(up))

    live = _live_for(project)
    if live:
        r.check("editor_offline", False, "桥仍活跃(编辑器开着)——cook 需先关闭编辑器再跑本脚本")
        return r.finish()
    r.check("editor_offline", True, "bridge 非活跃（编辑器已关，符合 cook 前置）")

    d = server.cook_package(platform="Windows", mode="cook", configuration="Development",
                            map=a.map, iterate=True, dry_run=True, confirm=False, project=project)
    dres = d.get("result") or {}
    cmd = dres.get("command", "")
    checks = dres.get("checks") or {}
    ok_dry = (d.get("ok") and dres.get("dry_run") is True
              and "BuildCookRun" in cmd and "-cook" in cmd and "-skipstage" in cmd
              and checks.get("runuat_found") is True and bool(checks.get("engine_root")))
    r.check("cook_dry_run_assembles", ok_dry,
            "runuat=%s engine_how=%s" % (checks.get("runuat_found"), checks.get("engine_root_how")))
    r.check("cook_dry_run_map_targeted", ('+maps="%s"' % a.map) in cmd, "+maps=%s 在命令里" % a.map)

    if a.dry:
        print("\n(--dry：跳过真实执行)")
        return r.finish()

    s = server.cook_package(platform="Windows", mode="cook", configuration="Development",
                            map=a.map, iterate=True, dry_run=False, confirm=True, project=project)
    if not s.get("ok"):
        r.check("cook_launch", False, "err=%s" % (s.get("error"),))
        return r.finish()
    task_id = (s.get("result") or {}).get("task_id")
    r.check("cook_launch", bool(task_id), "task_id=%s" % task_id)

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
    r.check("cook_reaches_done", final is not None, "status=%s" % (final or {}).get("status"))
    r.check("cook_succeeded", (final or {}).get("status") == "succeeded", "exit_code=%s" % exit_code)

    if not a.skip_artifact:
        umap = _cooked_umap(project, a.map)
        r.check("cooked_map_present", bool(umap),
                (os.path.relpath(umap, project) if umap else "Saved/Cooked 下未找到 %s.umap" % a.map))

    att = server.attribute_cook_errors(task_id=task_id, top=10, project=project)
    r.check("attribute_runs", att.get("ok"), "groups=%s" % (att.get("result") or {}).get("total"))

    return r.finish()


if __name__ == "__main__":
    sys.exit(main())
