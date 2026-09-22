"""v0.2 UAT 定位与 cook/package 提交（server 侧；PR-2，见 docs/DESIGN_v0.2.md）。

- 引擎根定位：env PRISM_ENGINE_ROOT 优先（源码版/Rocket 版没有注册表项）；
  否则 .uproject→EngineAssociation→注册表（HKLM EpicGames / WOW6432Node / HKCU Builds）。
- 首期 Windows-only（设计稿 §8-3）：其它平台由 platform 校验挡下，报 PACKAGE_ENV_MISSING。
- 命令模板：cook=BuildCookProject（默认 -iterate）；package=BuildCookRun（-build -cook -stage -pak -archive）。
- 提交执行 = subprocess.Popen 重定向日志到 tasks/<id>.log，登记 pid；
  本进程活到任务结束就 mark_done；server 中途死掉则由 derive_state 推导 abandoned，
  或（PR-2 起）get_cook_status 从日志尾部 RETURN: <code>（RunUAT 收束行）合成 done——跨重启恢复。
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time

from . import envelope, tasks

PLATFORMS = ("Windows",)  # 首期（设计稿 §8-3）
# RunUAT 收束行：如 "RETURN: # Success" / "RETURN: 1"；取首词做退出码推断
_RETURN_RE = re.compile(r"RETURN:\s*#?\s*(\d+|Success|Error)", re.IGNORECASE)


class UatError(Exception):
    def __init__(self, code, message):
        Exception.__init__(self, message)
        self.code = code
        self.message = message


def _registry_engine_dir(assoc):
    if sys.platform != "win32":
        return None
    import winreg
    keys = (
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\EpicGames\Unreal Engine\\" + assoc),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\EpicGames\Unreal Engine\\" + assoc),
        (winreg.HKEY_CURRENT_USER, r"Software\Epic Games\Unreal Engine\Builds\\" + assoc.replace("\\", r"")),
    )
    for hive, sub in keys:
        try:
            with winreg.OpenKey(hive, sub) as k:
                val, _ = winreg.QueryValueEx(k, "InstalledDirectory")
                if val and os.path.isdir(val):
                    return os.path.abspath(val)
        except OSError:
            continue
    return None


def find_uproject(project_dir):
    if not project_dir or not os.path.isdir(project_dir):
        return None
    for f in os.listdir(project_dir):
        if f.endswith(".uproject"):
            return os.path.join(project_dir, f)
    return None


def read_engine_association(uproject_path):
    import json
    try:
        with open(uproject_path, "r", encoding="utf-8-sig") as f:
            return json.load(f).get("EngineAssociation")
    except (OSError, ValueError):
        return None


def discover_engine_root(project_dir):
    """返回 (engine_root|None, how)。how 供 dry_run 回显定位路径。"""
    env_root = os.environ.get("PRISM_ENGINE_ROOT")
    if env_root:
        if os.path.isdir(env_root):
            return os.path.abspath(env_root), "env:PRISM_ENGINE_ROOT"
        raise UatError(envelope.Code.PACKAGE_ENV_MISSING,
                       "PRISM_ENGINE_ROOT set but not a dir: %s" % env_root)
    up = find_uproject(project_dir)
    if not up:
        raise UatError(envelope.Code.PACKAGE_ENV_MISSING,
                       "no .uproject under: %s" % project_dir)
    assoc = read_engine_association(up)
    if not assoc:
        raise UatError(envelope.Code.PACKAGE_ENV_MISSING,
                       "uproject has no EngineAssociation: %s" % up)
    root = _registry_engine_dir(assoc)
    if not root:
        raise UatError(
            envelope.Code.PACKAGE_ENV_MISSING,
            "engine %r not found in registry (source build? set PRISM_ENGINE_ROOT)" % assoc)
    return root, "registry:%s" % assoc


def runuat_path(engine_root):
    sub = "RunUAT.bat" if sys.platform == "win32" else "RunUAT.sh"
    return os.path.join(engine_root, "Engine", "Build", "BatchFiles", sub)


_PLATFORM_ALIAS = {"Windows": "Win64"}  # 5.4/5.8 真机校准：BuildCookRun 用构建平台名


def build_command(engine_root, uproject, platform, mode, configuration, map_name,
                  output_dir, iterate):
    """cook 与 package 都走 BuildCookRun（5.4 起无 BuildCookProject 命令，PR-4 真机校准）。
    cook = -cook -skipstage（只出 cooked 数据不打包）；-nop4 -unattended 防 UAT 交互挂起。
    """
    uat = runuat_path(engine_root)
    pf = _PLATFORM_ALIAS.get(platform, platform)
    argv = [uat, "BuildCookRun",
            '-project="%s"' % uproject,
            "-platform=%s" % pf,
            "-nocompileeditor", "-nop4", "-unattended",
            "-clientconfig=" + configuration]
    if mode == "package":
        argv += ["-build", "-cook", "-stage", "-pak", "-archive"]
        if output_dir:
            argv.append('-archivedirectory="%s"' % output_dir)
    else:
        argv += ["-cook", "-skipstage"]
        if iterate:
            argv.append("-iterate")
        if map_name:
            argv.append('+maps="%s"' % map_name)
    return argv


def check_environment(project_dir, platform):
    """dry_run 的环境体检（也用于真执行前置）。返回 (checks, engine_root, uproject, err_env)。"""
    checks = {"platform_supported": platform in PLATFORMS, "uproject_found": None,
              "engine_root": None, "engine_root_how": None}
    err = None
    try:
        up = find_uproject(project_dir)
        checks["uproject_found"] = up
        if not up:
            err = envelope.make_err(envelope.Code.PACKAGE_ENV_MISSING,
                                    "no .uproject under %s" % project_dir)
        else:
            root, how = discover_engine_root(project_dir)
            checks["engine_root"], checks["engine_root_how"] = root, how
            uat = runuat_path(root)
            checks["runuat_found"] = os.path.isfile(uat)
            if not checks["runuat_found"]:
                err = envelope.make_err(envelope.Code.PACKAGE_ENV_MISSING,
                                        "RunUAT not found: %s" % uat)
    except UatError as e:
        err = envelope.make_err(e.code, e.message)
    if platform not in PLATFORMS and err is None:
        err = envelope.make_err(envelope.Code.PACKAGE_ENV_MISSING,
                                "platform %r unsupported (first iteration: %s)" % (platform, list(PLATFORMS)))
    return checks, err


def scan_log_done(log_path):
    """从日志尾部找 RunUAT 收束行；返回 exit_code(int) 或 None。"""
    try:
        size = os.path.getsize(log_path)
        with open(log_path, "rb") as f:
            f.seek(max(0, size - 65536))
            tail = f.read().decode("utf-8", "replace")
    except OSError:
        return None
    code = None
    for m in _RETURN_RE.finditer(tail):
        code = m.group(1)
    if code is None:
        return None
    return 0 if code.lower() in ("success", "0") else (1 if code.lower() == "error" else int(code))


def submit(bus_dir, project_dir, platform, mode, configuration, map_name, output_dir,
           iterate, command_builder=None, popen=None):
    """真执行路径（confirm 已由上层把门）。返回 ok 信封或错误信封。"""
    up = find_uproject(project_dir)
    try:
        root, _how = discover_engine_root(project_dir)
    except UatError as e:
        return envelope.make_err(e.code, e.message)
    build = command_builder or build_command
    argv = build(root, up, platform, mode, configuration, map_name, output_dir, iterate)
    log_path = os.path.join(bus_dir, "tasks", "%s.pending.log" % os.getpid())
    try:
        rec = tasks.create(bus_dir, mode, project_dir, " ".join(argv), log_path)
    except tasks.TaskError as e:
        return envelope.make_err(e.code, e.message)
    rec["log_path"] = os.path.join(bus_dir, "tasks", rec["task_id"] + ".log")
    tasks._write(bus_dir, tasks._rec_path(bus_dir, rec["task_id"]), rec)
    try:
        log_fh = open(rec["log_path"], "wb", 0)
        run = popen or (lambda a, fh: subprocess.Popen(a, stdout=fh, stderr=subprocess.STDOUT,
                                                       cwd=os.path.dirname(a[0])))
        proc = run(argv, log_fh)
    except OSError as e:
        rec["note"] = "launch failed: %s" % e
        tasks._write(bus_dir, tasks._rec_path(bus_dir, rec["task_id"]), rec)
        return envelope.make_err(envelope.Code.RUNTIME_ERROR, "launch failed: %s" % e)
    rec = tasks.attach_launch(bus_dir, rec, proc.pid)
    _LIVE[rec["task_id"]] = (proc, log_fh)
    return envelope.make_ok(tasks.view(bus_dir, rec["task_id"]))


_LIVE = {}  # task_id -> (Popen, log_fh)，本 server 进程内


def poll_done(bus_dir, task_id):
    """有机会就把「已知结局」写进 done 标记（尽力而为，非必须——derive 兜底）。"""
    entry = _LIVE.get(task_id)
    if entry is not None:
        proc, fh = entry
        rc = proc.poll()
        if rc is not None:
            try:
                fh.close()
            except OSError:
                pass
            del _LIVE[task_id]
            tasks.mark_done(bus_dir, task_id, rc)
            return
    # 跨重启恢复：done 缺失但日志已有收束行 -> 合成
    try:
        rec = tasks.load(bus_dir, task_id)
    except tasks.TaskError:
        return
    status, _ = tasks.derive_state(bus_dir, rec)
    if status in (tasks.STATUS_RUNNING, tasks.STATUS_PENDING, tasks.STATUS_FAILED):
        code = scan_log_done(rec.get("log_path"))
        if code is not None:
            tasks.mark_done(bus_dir, task_id, code)


def log_tail(log_path, lines):
    try:
        size = os.path.getsize(log_path)
        with open(log_path, "rb") as f:
            f.seek(max(0, size - 200 * max(1, int(lines))))
            data = f.read().decode("utf-8", "replace")
    except OSError:
        return []
    return data.splitlines()[-int(lines):]
