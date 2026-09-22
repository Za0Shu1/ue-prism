"""ue-prism 命令行：`prism`（serve/setup）。

- `prism serve [...]`            直接跑 MCP server（等价 ue-prism / python -m prism.server）。
- `prism setup --client ...`     把「全局、零工程路径」的 MCP 注册项写进客户端配置（幂等）。
- `prism setup --uninstall ...`  从客户端配置移除该项。

设计（docs/DESIGN_v1.0.md §4）：Codex ~/.codex/config.toml、opencode ~/.config/opencode/opencode.jsonc、
Cursor ~/.cursor/mcp.json。一律原子替换、不覆盖用户其它条目；无法安全解析的配置退回「打印待粘贴块」而不强改。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

SERVER_ARGS = ["-m", "prism.server"]
SERVER_KEY = "ue-prism"
CLIENTS = ("codex", "opencode", "cursor", "claude", "claude-code")
STARTUP_TIMEOUT_SEC = 20  # Codex 客户端启动等待上限（秒）；与工程路径无关


def _server_args(project_dir=None, bus_dir=None):
    """默认零工程路径（靠 registry 自动发现）；给了 --project-dir/--bus-dir 则钉死单工程（向后兼容显式写法）。"""
    args = list(SERVER_ARGS)
    if project_dir:
        args += ["--project-dir", project_dir]
    if bus_dir:
        args += ["--bus-dir", bus_dir]
    return args


def _esc(s):
    return json.dumps(s)


def _esc_list(items):
    return "[" + ", ".join(_esc(x) for x in items) + "]"


def toml_block(py, args=None, timeout_sec=STARTUP_TIMEOUT_SEC):
    args = list(SERVER_ARGS if args is None else args)
    block = ("[mcp_servers.%s]\n" % SERVER_KEY
             + "command = %s\n" % _esc(py)
             + "args = %s\n" % _esc_list(args))
    if timeout_sec:
        block += "startup_timeout_sec = %d" % int(timeout_sec)
    return block


def toml_ensure(text, py, args=None, timeout_sec=STARTUP_TIMEOUT_SEC):
    if text is None:
        text = ""
    stripped, _ = toml_remove(text)
    merged = stripped
    block = toml_block(py, args, timeout_sec)
    if merged.strip():
        merged = merged.rstrip("\n") + "\n\n" + block
    else:
        merged = block
    return merged, (merged != (text or ""))


def toml_remove(text):
    if not text:
        return text or "", False
    lines = text.split("\n")
    out = []
    i = 0
    removed = False
    header_pat = re.compile(r"^\s*\[mcp_servers\." + re.escape(SERVER_KEY) + r"\]\s*$")
    section_pat = re.compile(r"^\s*\[.*\]\s*$")
    while i < len(lines):
        if header_pat.match(lines[i]):
            removed = True
            i += 1
            while i < len(lines) and not section_pat.match(lines[i]):
                i += 1
            continue
        out.append(lines[i])
        i += 1
    return "\n".join(out), removed


def _strip_json_comments(text):
    out = []
    i = 0
    n = len(text)
    in_str = False
    quote = ""
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1]); i += 2; continue
            if c == quote:
                in_str = False
            i += 1
            continue
        if c in ('"', "'"):
            in_str = True; quote = c; out.append(c); i += 1; continue
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _opencode_entry(py, args=None):
    args = list(SERVER_ARGS if args is None else args)
    return {"type": "local", "command": [py] + args, "enabled": True}


def _cursor_entry(py, args=None):
    args = list(SERVER_ARGS if args is None else args)
    return {"command": py, "args": args}


def _claudecode_entry(py, args=None):
    args = list(SERVER_ARGS if args is None else args)
    return {"type": "stdio", "command": py, "args": args}


def json_ensure(text, client, py, args=None):
    root = {}
    if text and text.strip():
        try:
            root = json.loads(text)
        except Exception:
            try:
                root = json.loads(_strip_json_comments(text))
            except Exception:
                return None, False, True
    if not isinstance(root, dict):
        return None, False, True
    if client == "opencode":
        root.setdefault("mcp", {})
        if not isinstance(root["mcp"], dict):
            return None, False, True
        root["mcp"][SERVER_KEY] = _opencode_entry(py, args)
    else:
        root.setdefault("mcpServers", {})
        if not isinstance(root["mcpServers"], dict):
            return None, False, True
        entry = _claudecode_entry(py, args) if client == "claude-code" else _cursor_entry(py, args)
        root["mcpServers"][SERVER_KEY] = entry
    merged = json.dumps(root, indent=2, ensure_ascii=False) + "\n"
    return merged, (merged != (text or "")), False


def json_remove(text, client):
    root = {}
    if text and text.strip():
        try:
            root = json.loads(text)
        except Exception:
            try:
                root = json.loads(_strip_json_comments(text))
            except Exception:
                return None, False, True
    if not isinstance(root, dict):
        return None, False, True
    removed = False
    container = "mcp" if client == "opencode" else "mcpServers"
    if isinstance(root.get(container), dict) and SERVER_KEY in root[container]:
        del root[container][SERVER_KEY]
        removed = True
    return json.dumps(root, indent=2, ensure_ascii=False) + "\n", removed, False


def default_path(client):
    home = Path.home()
    if client == "codex":
        return home / ".codex" / "config.toml"
    if client == "opencode":
        return home / ".config" / "opencode" / "opencode.jsonc"
    if client == "cursor":
        return home / ".cursor" / "mcp.json"
    if client == "claude":
        if os.name == "nt":
            appdata = os.environ.get("APPDATA") or str(home / "AppData" / "Roaming")
            return Path(appdata) / "Claude" / "claude_desktop_config.json"
        return home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    if client == "claude-code":
        # Claude Code 项目级共享配置（放当前工作目录；避免改含登录态的 ~/.claude.json）
        return Path.cwd() / ".mcp.json"
    raise ValueError("unknown client: %r" % client)


def _read(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return None


def _write_atomic(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)


def _apply(client, text, py, args, timeout_sec, uninstall):
    if client == "codex":
        if uninstall:
            new, changed = toml_remove(text or "")
            return (new if changed else text), changed, False
        new, changed = toml_ensure(text, py, args, timeout_sec)
        return new, changed, False
    if uninstall:
        new, changed, needs = json_remove(text, client)
        return (new if changed else text), changed, needs
    return json_ensure(text, client, py, args)


def setup_client(client, py=None, uninstall=False, dry_run=False, path=None,
                 project_dir=None, bus_dir=None, timeout_sec=STARTUP_TIMEOUT_SEC):
    py = py or sys.executable
    args = _server_args(project_dir, bus_dir)
    target = Path(path) if path else default_path(client)
    text = _read(target)
    new, changed, needs_manual = _apply(client, text, py, args, timeout_sec, uninstall)
    result = {"client": client, "path": str(target), "existed": text is not None,
              "uninstall": bool(uninstall), "changed": bool(changed),
              "needs_manual": bool(needs_manual), "dry_run": bool(dry_run)}
    if needs_manual:
        result["hint"] = "配置文件无法安全解析（可能是手写 JSONC）；请手动加入下方注册块。"
        result["snippet"] = _snippet(client, py, args, timeout_sec, uninstall)
        return result
    if dry_run and changed:
        result["snippet"] = _snippet(client, py, args, timeout_sec, uninstall)
    if changed and not dry_run:
        _write_atomic(target, new)
        result["written"] = True
    return result


def _snippet(client, py, args=None, timeout_sec=STARTUP_TIMEOUT_SEC, uninstall=False):
    if uninstall:
        return "（卸载：请从配置中手动删除 ue-prism 条目）"
    args = SERVER_ARGS if args is None else args
    if client == "codex":
        return toml_block(py, args, timeout_sec)
    if client == "opencode":
        entry = _opencode_entry(py, args)
    elif client == "claude-code":
        entry = _claudecode_entry(py, args)
    else:
        entry = _cursor_entry(py, args)
    container = "mcp" if client == "opencode" else "mcpServers"
    return json.dumps({container: {SERVER_KEY: entry}}, indent=2, ensure_ascii=False)


def _print_result(r):
    if r.get("needs_manual"):
        print("[%s] %s -> 需手动：%s" % (r["client"], r["path"], r.get("hint", "")))
        print("----- 待粘贴注册块 -----\n%s\n------------------------" % r.get("snippet", ""))
        return
    if not r.get("changed"):
        print("[%s] %s -> 已是目标状态，无需改动" % (r["client"], r["path"]))
        return
    verb = ("移除该项" if r.get("uninstall") else ("更新该项" if r.get("existed") else "新增该项"))
    if r.get("dry_run"):
        print("[%s] %s -> 将%s（dry-run 仅预览，未写盘）：" % (r["client"], r["path"], verb))
        print("----- 预览：将要写入的注册块 -----")
        print(r.get("snippet", ""))
        print("----------------------------------")
    else:
        print("[%s] %s -> 已%s" % (r["client"], r["path"], verb))


def cmd_setup(args):
    clients = list(CLIENTS) if args.client == "all" else [args.client]
    py = args.python or sys.executable
    for c in clients:
        _print_result(setup_client(c, py=py, uninstall=args.uninstall,
                                   dry_run=args.dry_run, path=args.path,
                                   project_dir=args.project_dir, bus_dir=args.bus_dir,
                                   timeout_sec=args.startup_timeout_sec))
        if c == "claude-code" and not args.uninstall:
            print("  等价（用户级全局，直接跑 CLI，不改文件）：")
            print('  claude mcp add %s --scope user -- %s %s'
                  % (SERVER_KEY, _esc(py), " ".join(SERVER_ARGS)))
    return 0


def cmd_plugin_build(args):
    from . import pluginpack
    dest = os.path.join(args.out, pluginpack.PLUGIN_NAME)
    r = pluginpack.assemble(dest, version=args.version, force=args.force)
    if not r.get("ok"):
        print("[plugin-build] 未写：%s" % r.get("note", r))
        return 2
    print("[plugin-build] 已生成 %d 个文件 -> %s" % (r["files_written"], r["plugin_root"]))
    print("  分发：把该目录放进 UE 工程的 Plugins/ 下（或直接用 prism plugin-install --project <P>）。")
    return 0


def cmd_plugin_install(args):
    from . import pluginpack
    root = pluginpack.plugin_root_for_project(args.project)
    if not args.yes:
        pl = pluginpack.plan(root, version=args.version)
        print("[plugin-install] dry-run（未写盘）目标：%s" % pl["plugin_root"])
        print("  版本=%s | %s" % (pl["version"], pl["note"]))
        print("  将产出 %d 个文件：" % len(pl["files"]))
        for f in pl["files"]:
            print("    - " + f)
        print("  确认后加 --yes 落盘（目标已存在再加 --force 覆盖）。")
        return 0
    r = pluginpack.assemble(root, version=args.version, force=args.force)
    if not r.get("ok"):
        print("[plugin-install] 未写：%s  （加 --force 覆盖）" % r.get("note", r))
        return 2
    print("[plugin-install] 已装 %d 个文件 -> %s" % (r["files_written"], r["plugin_root"]))
    print("  下一步：在 UE 打开该工程 -> 插件 UEPrism（默认启用）会自启 bridge。")
    print("  若之前用 StartupScripts/手动 [PY] 起过 bridge，二选一即可，避免重复轮询。")
    return 0


def build_parser():
    ap = argparse.ArgumentParser(prog="prism", description="ue-prism MCP server 与安装器")
    sub = ap.add_subparsers(dest="cmd")
    p_serve = sub.add_parser("serve", help="运行 MCP server")
    p_serve.add_argument("--project-dir")
    p_serve.add_argument("--bus-dir")
    p_serve.add_argument("--transport", default="stdio")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8765)
    p_setup = sub.add_parser("setup", help="把 MCP 注册项写进客户端配置（幂等）")
    p_setup.add_argument("--client", required=True, choices=list(CLIENTS) + ["all"])
    p_setup.add_argument("--uninstall", action="store_true")
    p_setup.add_argument("--dry-run", action="store_true", dest="dry_run")
    p_setup.add_argument("--python", dest="python")
    p_setup.add_argument("--path")
    p_setup.add_argument("--project-dir", dest="project_dir", help="可选：把 server 钉死到该 UE 工程（否则靠 registry 自动发现）")
    p_setup.add_argument("--bus-dir", dest="bus_dir", help="可选：显式总线目录（配合 --project-dir）")
    p_setup.add_argument("--startup-timeout-sec", dest="startup_timeout_sec", type=int,
                         default=STARTUP_TIMEOUT_SEC, help="写入 Codex 的 startup_timeout_sec（仅 codex 客户端生效，默认 20；0 关闭）")
    p_pb = sub.add_parser("plugin-build", help="把引擎侧模块打包成免编译 UE 插件 UEPrism（可分发目录）")
    p_pb.add_argument("--out", required=True, help="输出父目录，将在其下生成 UEPrism/")
    p_pb.add_argument("--version", default=None, help="插件 VersionName（默认取 prism.__version__）")
    p_pb.add_argument("--force", action="store_true", help="目标已存在时覆盖")
    p_pi = sub.add_parser("plugin-install", help="把 UEPrism 插件装进某个 UE 工程的 Plugins/ 目录")
    p_pi.add_argument("--project", required=True, help="UE 工程根目录（含 .uproject 的目录）")
    p_pi.add_argument("--version", default=None)
    p_pi.add_argument("--yes", action="store_true", help="确认写盘（默认仅 dry-run 预览）")
    p_pi.add_argument("--force", action="store_true", help="目标已存在时覆盖")
    return ap


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    ap = build_parser()
    args = ap.parse_args(argv)
    if args.cmd == "setup":
        return cmd_setup(args)
    if args.cmd == "serve":
        from . import server as _srv
        fwd = []
        if getattr(args, "project_dir", None):
            fwd += ["--project-dir", args.project_dir]
        if getattr(args, "bus_dir", None):
            fwd += ["--bus-dir", args.bus_dir]
        fwd += ["--transport", args.transport, "--host", args.host, "--port", str(args.port)]
        return _srv.main(fwd)
    if args.cmd == "plugin-build":
        return cmd_plugin_build(args)
    if args.cmd == "plugin-install":
        return cmd_plugin_install(args)
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())