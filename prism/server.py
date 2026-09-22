"""MCP server：把工具暴露给 MCP 客户端。server 侧不碰引擎 API（见 docs/REQUIREMENTS.md §2）。

- 桥工具（ping / list_level_actors / describe_asset / get_asset_references / get_asset_metrics）：
  经文件总线问编辑器内的 bridge（需编辑器在线）。
- 离线工具（read_editor_log / scan_folder_assets）：读磁盘（日志 / Content .uasset），不依赖 bridge。
- cook 工具族：server 侧 shell 调 UAT（编辑器须关）；写操作，双钥确认。
- 连接模型（v1.0）：MCP 配置里不写死工程路径；“连哪个工程”由 _resolve() 判定阶梯在运行期决定
  （project= 选择器 > 显式配置/默认 > 单一活跃工程自动 > 0=NO_PROJECT / 多=AMBIGUOUS）。见 DESIGN_v1.0 §3。

传输：默认 stdio（agent 直接 spawn 本进程）。可选 --transport http 起本地端口。
依赖 MCP Python SDK（锁 2.x）。SDK v2 里 FastMCP 已更名 MCPServer，_make_server 对可能路径做兼容。
"""
from __future__ import annotations

import argparse
import os
import re
import traceback
from typing import Annotated

from pydantic import Field

from . import attributelog, bus, envelope, folderscan, logscan, registry, rules, tasks, uat

# 运行期配置：main() 从命令行/环境变量初始化；_resolve 读取此处做“显式配置”分支（向后兼容）。
_CFG = {"bus_dir": None, "project_dir": None}


def _as_bool(v, default=False):
    """MCP 通道把 bool 参数以字符串送达："False"/"0" 等恒为真值——双钥契约的 truthy 陷阱。

    显式归一化：true/1/yes/on -> True；false/0/no/off/空 -> False；None -> default；其余报错。
    """
    if isinstance(v, bool):
        return v
    if v is None:
        return default
    t = str(v).strip().lower()
    if t in ("1", "true", "yes", "y", "on"):
        return True
    if t in ("", "0", "false", "no", "n", "off"):
        return False
    raise ValueError("无法解析布尔值: %r（可用 true/false/1/0/yes/no）" % (v,))


# ---------------- 连接解析：判定阶梯（DESIGN_v1.0 §3.3） ----------------

def _has_explicit():
    return bool(_CFG["bus_dir"] or _CFG["project_dir"]
                or os.environ.get("PRISM_BUS_DIR") or os.environ.get("PRISM_PROJECT_DIR"))


def _explicit():
    b = _CFG["bus_dir"] or os.environ.get("PRISM_BUS_DIR")
    p = _CFG["project_dir"] or os.environ.get("PRISM_PROJECT_DIR")
    if not b and p:
        b = os.path.join(p, "Saved", "Prism")
    if not p and b:
        p = registry.project_from_bus(b)
    return b, p


def _resolve(project=None, allow_stale=False):
    """把“连哪个工程”解析成 (bus_dir, project_dir, err)；err 为 None 或错误信封。

    判定阶梯：project= 选择器 > 显式配置/默认 pin > 单一活跃工程自动 > 0/多 报错。
    点名 project 但注册表无匹配 -> PROJECT_NOT_FOUND（不再静默回落到默认 pin）。
    allow_stale=True：默认“唯一工程”从全部注册工程里取（含编辑器已关），供 cook / 性能报告
    等“关编辑器 / 纯离线”场景；False：只看活跃工程（桥工具）。
    """
    if project:  # 调用方点名优先：即使有默认 pin 也能连另一个活跃工程（v1.1 加固）
        entry = registry.find(project)
        if not entry:
            return None, None, envelope.make_err(
                envelope.Code.PROJECT_NOT_FOUND,
                "no registered project matches %r; registered=%s"
                % (project, registry.names() or "[]"),
            )
        return entry["bus_dir"], entry["project_dir"], None
    if _has_explicit():
        b, p = _explicit()
        return b, p, None
    pool = registry.discover() if allow_stale else registry.live_entries()
    if len(pool) == 1:
        return pool[0]["bus_dir"], pool[0]["project_dir"], None
    if len(pool) == 0:
        if not allow_stale and registry.discover():
            return None, None, envelope.make_err(
                envelope.Code.NO_PROJECT,
                "registered project(s) %s are offline (stale heartbeat); open the editor"
                % registry.names(),
            )
        return None, None, envelope.make_err(
            envelope.Code.NO_PROJECT,
            "no live bridge: open the UE editor with the prism plugin enabled",
        )
    return None, None, envelope.make_err(
        envelope.Code.AMBIGUOUS_PROJECT,
        "multiple live projects, pass project=: %s" % registry.names(pool),
    )


def _resolve_offline_dir(project_dir=None, project=None):
    """离线工具用：显式 project_dir 优先，否则从 project / registry 解析；返回 (pd, err)。"""
    if project_dir:
        return project_dir, None
    _b, pd, err = _resolve(project, allow_stale=True)
    if err:
        return None, err
    if not pd:
        return None, envelope.make_err(
            envelope.Code.RUNTIME_ERROR,
            "no project_dir (pass project= / --project-dir, or open an editor with the plugin)")
    return pd, None


# ---------------- 桥工具（编辑器在线） ----------------

def ping(project: Annotated[str | None, Field(description="工程选择器：名称/路径/slug；仅一个活跃工程时可省略")] = None):
    """Ping the in-editor bridge: heartbeat, UE version, project dir, loaded maps."""
    bus_dir, _pd, err = _resolve(project)
    if err:
        return err
    return bus.BusClient(bus_dir, timeout=bus.DEFAULT_TIMEOUT).call("ping", {})


def list_level_actors(class_contains: Annotated[str | None, Field(description="按 Actor 类名子串过滤（不区分大小写）")] = None, tag: Annotated[str | None, Field(description="按 Actor tag 过滤")] = None, limit: Annotated[int, Field(description="最多返回的 Actor 数（截断带 truncated 标记）")] = 200, project: Annotated[str | None, Field(description="工程选择器：名称/路径/slug；仅一个活跃工程时可省略")] = None):
    """List actors in the loaded level (name/class/label/path/location/components). Needs the live bridge."""
    bus_dir, _pd, err = _resolve(project)
    if err:
        return err
    return bus.BusClient(bus_dir, timeout=bus.DEFAULT_TIMEOUT).call(
        "list_level_actors",
        {"class_contains": class_contains, "tag": tag, "limit": limit},
    )


def describe_asset(asset_path: Annotated[str, Field(description="/Game 包路径，如 /Game/Foo/Bar")], project: Annotated[str | None, Field(description="工程选择器：名称/路径/slug；仅一个活跃工程时可省略")] = None):
    """Read one asset's registry metadata (class/package/on-disk size). Needs the live bridge."""
    bus_dir, _pd, err = _resolve(project)
    if err:
        return err
    return bus.BusClient(bus_dir, timeout=bus.DEFAULT_TIMEOUT).call(
        "describe_asset", {"asset_path": asset_path})


def get_asset_references(asset_path: Annotated[str, Field(description="/Game 包路径，如 /Game/Foo/Bar")], direction: Annotated[str, Field(description="uses=依赖它的 | used_by=引用它的 | both=双向")] = "both", recursive: Annotated[bool, Field(description="true=递归展开依赖闭包（深度/节点数有上限）")] = False, limit: Annotated[int, Field(description="每方向返回条数上限")] = 500, project: Annotated[str | None, Field(description="工程选择器：名称/路径/slug；仅一个活跃工程时可省略")] = None):
    """Reference graph via AssetRegistry: uses(依赖)/used_by(被引用). direction in both|uses|used_by. Needs live bridge."""
    bus_dir, _pd, err = _resolve(project)
    if err:
        return err
    return bus.BusClient(bus_dir, timeout=bus.DEFAULT_TIMEOUT).call(
        "get_asset_references",
        {"asset_path": asset_path, "direction": direction, "recursive": recursive, "limit": limit},
    )


def get_asset_chain(asset_path: Annotated[str, Field(description="/Game 包路径，如 /Game/Foo/Bar")], direction: Annotated[str, Field(description="uses（要带走的依赖链）| used_by（连累面）")] = "uses", scope: Annotated[str, Field(description="game=只含 /Game（默认）| all=含引擎资产")] = "game", with_meta: Annotated[bool, Field(description="true=逐节点补类名/磁盘大小并按类别聚合")] = True, max_nodes: Annotated[int, Field(description="闭包节点总数上限")] = 2000, max_depth: Annotated[int, Field(description="0=不限深度；>0 限制递归层数")] = 0, project: Annotated[str | None, Field(description="工程选择器：名称/路径/slug；仅一个活跃工程时可省略")] = None):
    """递归依赖闭包（迁移预览）：uses=该资产依赖的整条链(迁移要带走)；used_by=谁依赖它(连累面)。
    scope=game(默认)只留 /Game；with_meta 给每节点补 class+size_bytes 并按类别聚合体量。Needs live bridge."""
    bus_dir, _pd, err = _resolve(project)
    if err:
        return err
    return bus.BusClient(bus_dir, timeout=bus.DEFAULT_TIMEOUT).call(
        "get_asset_chain",
        {"asset_path": asset_path, "direction": direction, "scope": scope, "with_meta": with_meta,
         "max_nodes": max_nodes, "max_depth": max_depth})


def _as_path_list(value):
    """Normalize asset_paths: list/tuple as-is, or a string split on , / ; / newline."""
    if not value:
        return []
    if isinstance(value, (list, tuple)):
        seq = list(value)
    else:
        seq = re.split(r"[,\n;]+", str(value))
    return [t for t in (str(x).strip() for x in seq) if t]


def get_asset_metrics(asset_paths: Annotated[str, Field(description="多个 /Game 路径，支持逗号/分号/换行分隔")], max_assets: Annotated[int, Field(description="本次最多载入编辑器内存的资产数（保持小值）")] = 20, project: Annotated[str | None, Field(description="工程选择器：名称/路径/slug；仅一个活跃工程时可省略")] = None):
    """Batch raw asset metrics via bridge (texture px / mesh LOD triangles), with tried/api evidence.

    Loads assets into editor memory as a side effect - keep max_assets small. Needs live bridge.
    """
    bus_dir, _pd, err = _resolve(project)
    if err:
        return err
    return bus.BusClient(bus_dir, timeout=bus.DEFAULT_TIMEOUT).call(
        "get_asset_metrics", {"asset_paths": _as_path_list(asset_paths), "max_assets": int(max_assets)})


def scan_orphan_assets(folder: Annotated[str, Field(description="扫描范围，Content 下目录前缀（默认 /Game）")] = "/Game", limit: Annotated[int, Field(description="本窗口最多检查的资产数")] = 2000, offset: Annotated[int, Field(description="分页偏移")] = 0, max_orphans: Annotated[int, Field(description="孤儿输出上限（按 size 降序截断）")] = 200, recent_days: Annotated[int, Field(description="近 N 天有改动的标记 suspect（删除前须二次校验）")] = 14, cascade: Annotated[bool, Field(description="true=追加级联可回收上限（仅被孤儿引用的依赖）")] = True, project: Annotated[str | None, Field(description="工程选择器：名称/路径/slug；仅一个活跃工程时可省略")] = None):
    """扫描 /Game 孤儿资产（引用图里无任何引用者）：给可回收体量 + 按目录聚合 + 逐条 size/可信度。只读。Needs live bridge.

    工程越大越慢：limit 控制本窗口最多检查多少资产，offset 翻页；orphans 按 size 降序截到 max_orphans。
    confidence=suspect 者（可能按名/软路径加载或刚改动）删除前务必二次校验+走版本管理。
    """
    bus_dir, _pd, err = _resolve(project)
    if err:
        return err
    return bus.BusClient(bus_dir, timeout=bus.DEFAULT_TIMEOUT).call(
        "scan_orphan_assets",
        {"folder": folder, "limit": int(limit), "offset": int(offset),
         "max_orphans": int(max_orphans), "recent_days": int(recent_days),
         "cascade": cascade})

# ---------------- 离线工具（读磁盘） ----------------

def read_editor_log(level: Annotated[str, Field(description="Error|Warning|Display|All")] = "Error", tail: Annotated[int, Field(description="从日志末尾读取的行数")] = 2000, top: Annotated[int, Field(description="聚合分组上限")] = 30, project_dir: Annotated[str | None, Field(description="显式工程目录（与 project= 二选一）")] = None, project: Annotated[str | None, Field(description="工程选择器：名称/路径/slug；仅一个活跃工程时可省略")] = None):
    """Aggregate editor log lines by message (category/count/first_seen/sample). Offline; no bridge needed."""
    pd, err = _resolve_offline_dir(project_dir, project)
    if err:
        return err
    try:
        return envelope.make_ok(logscan.read_editor_log(pd, level=level, tail=int(tail), top=int(top)))
    except FileNotFoundError as e:
        return envelope.make_err(envelope.Code.RUNTIME_ERROR, str(e))
    except Exception as e:
        return envelope.make_err(envelope.Code.RUNTIME_ERROR, str(e), traceback.format_exc())


def scan_folder_assets(folder: Annotated[str, Field(description="Content 下文件夹，/Game 前缀可省略")] = "/Game", sort: Annotated[str, Field(description="size（默认）| path")] = "size", limit: Annotated[int, Field(description="返回条数上限")] = 500, project_dir: Annotated[str | None, Field(description="显式工程目录（与 project= 二选一）")] = None, project: Annotated[str | None, Field(description="工程选择器：名称/路径/slug；仅一个活跃工程时可省略")] = None):
    """Audit on-disk assets under <project>/Content: sorted by size + per-type totals. Offline; no bridge."""
    pd, err = _resolve_offline_dir(project_dir, project)
    if err:
        return err
    try:
        return envelope.make_ok(folderscan.scan_folder_assets(pd, folder=folder, sort=sort, limit=int(limit)))
    except FileNotFoundError as e:
        return envelope.make_err(envelope.Code.RUNTIME_ERROR, str(e))
    except Exception as e:
        return envelope.make_err(envelope.Code.RUNTIME_ERROR, str(e), traceback.format_exc())


# ---------------- cook / package（写操作，双钥确认） ----------------

def cook_package(platform: Annotated[str, Field(description="当前仅支持 Windows")] = "Windows", mode: Annotated[str, Field(description="cook | package")] = "cook", configuration: Annotated[str, Field(description="构建配置，如 Development/Shipping")] = "Development", map: Annotated[str | None, Field(description="要 cook 的地图（如 /Game/NewMap）；错误地图名会被 UE 静默忽略，先用 ping 核对 loaded_maps")] = None,
                 output_dir: Annotated[str | None, Field(description="package 模式的输出目录")] = None, iterate: Annotated[bool | None, Field(description="增量开关；缺省 cook=True / package=False")] = None, dry_run: Annotated[bool, Field(description="默认 true=只回预览/计划，不落盘不启动")] = True, confirm: Annotated[bool, Field(description="真实执行需 dry_run=false 且 confirm=true 双钥（写操作契约）")] = False, project: Annotated[str | None, Field(description="工程选择器：名称/路径/slug；仅一个活跃工程时可省略")] = None):
    """Trigger UE cook/package via UAT on the server side (first iteration Windows-only).

    mode: cook | package. DEFAULT dry_run: only returns assembled command + env checks.
    NOTE(5.4/5.8 real-machine calibration): a wrong +maps value is silently ignored by
    BuildCookRun (cook succeeds having cooked nothing) - verify map names via ping/list first.
    Also NOTE: UE cook is lenient - packages missing on disk only log Warnings and get
    silently dropped from the cooked map (BUILD SUCCESSFUL != content-complete); check
    read_editor_log/attribute output at Warning level when in doubt.
    Real execution REQUIRES BOTH dry_run=False AND confirm=True (write-op contract).
    Returns {dry_run, command, checks} or, when launched, a task view with task_id.
    """
    bus_dir, pd, err = _resolve(project, allow_stale=True)
    if err:
        return err
    if not pd:
        return envelope.make_err(envelope.Code.RUNTIME_ERROR, "no project_dir (pass project= / --project-dir)")
    if mode not in ("cook", "package"):
        return envelope.make_err(envelope.Code.RUNTIME_ERROR, "mode must be cook|package, got %r" % (mode,))
    try:
        dry_run = _as_bool(dry_run, default=True)
        confirm = _as_bool(confirm, default=False)
        iterate = None if iterate is None else _as_bool(iterate)
    except ValueError as e:
        return envelope.make_err(envelope.Code.RUNTIME_ERROR, str(e))
    if iterate is None:
        iterate = (mode == "cook")  # cook 默认增量；package 默认全量（设计稿 §4 注记）
    checks, uerr = uat.check_environment(pd, platform)
    if uerr is not None:
        return uerr
    argv = uat.build_command(checks["engine_root"], checks["uproject_found"], platform, mode,
                             configuration, map, output_dir, bool(iterate))
    if dry_run or not confirm:
        return envelope.make_ok({
            "dry_run": True,
            "command": " ".join(argv),
            "checks": checks,
            "note": "nothing executed; rerun with dry_run=False AND confirm=True to launch",
        })
    if not bus_dir:
        return envelope.make_err(envelope.Code.RUNTIME_ERROR, "no bus_dir for task registry")
    try:
        tasks.sweep_finished(bus_dir)
    except Exception:
        pass
    return uat.submit(bus_dir, pd, platform, mode, configuration, map, output_dir, bool(iterate))


def get_cook_status(task_id: Annotated[str, Field(description="cook_package 返回的任务 id")], tail: Annotated[int, Field(description="日志尾部行数")] = 20, project: Annotated[str | None, Field(description="工程选择器：名称/路径/slug；仅一个活跃工程时可省略")] = None):
    """Lazy-derived status of a cook/package task: {status, exit_code?, log, log_tail}.

    Status is recomputed from on-disk facts at read time (done marker / deadline /
    pid / RunUAT 'RETURN:' log line) - no background daemon involved.
    """
    bus_dir, _pd, err = _resolve(project, allow_stale=True)
    if err:
        return err
    if not bus_dir:
        return envelope.make_err(envelope.Code.RUNTIME_ERROR, "no bus_dir for task registry")
    try:
        uat.poll_done(bus_dir, task_id)
        v = tasks.view(bus_dir, task_id)
    except tasks.TaskError as e:
        return envelope.make_err(e.code, e.message)
    if v["log"].get("exists"):
        v["log_tail"] = uat.log_tail(v["log"]["path"], int(tail))
    return envelope.make_ok(v)


def attribute_cook_errors(task_id: Annotated[str | None, Field(description="cook 任务 id（与 log_path 二选一）")] = None, log_path: Annotated[str | None, Field(description="显式日志文件路径（与 task_id 二选一）")] = None, top: Annotated[int, Field(description="错误分组上限")] = 30, project: Annotated[str | None, Field(description="工程选择器：名称/路径/slug；仅一个活跃工程时可省略")] = None):
    """Group cook/editor log Error lines by fingerprint and attribute them to /Game assets.

    Provide task_id (from cook_package) or an explicit log_path. Offline-safe; when the
    bridge is live, assets are batch-enriched (class / used_by_count) in single bus round-trips.
    """
    if not task_id and not log_path:
        return envelope.make_err(envelope.Code.RUNTIME_ERROR, "need task_id or log_path")
    bus_dir, proj, _rerr = _resolve(project, allow_stale=True)
    bus_dir = bus_dir or ""
    proj = proj or ""
    lp = log_path
    if task_id:
        if not bus_dir:
            return envelope.make_err(envelope.Code.RUNTIME_ERROR, "no bus_dir")
        try:
            lp = tasks.load(bus_dir, task_id)["log_path"]
        except tasks.TaskError as e:
            return envelope.make_err(e.code, e.message)
    if not lp or not os.path.isfile(lp):
        return envelope.make_err(envelope.Code.RUNTIME_ERROR, "no log file: %s" % lp)
    try:
        report = attributelog.parse_log(lp, project_dir=proj, top=int(top))
    except (OSError, ValueError) as e:
        return envelope.make_err(envelope.Code.RUNTIME_ERROR, str(e))
    if bus_dir:
        emap, note = attributelog.enrich_via_bus(bus_dir, attributelog.all_assets(report))
        attributelog.apply_enrichment(report, emap, note)
    return envelope.make_ok(report)


# ---------------- 性能规则 ----------------

def get_perf_report(scope: Annotated[str, Field(description="必须是 Content 下真实存在的文件夹（/Game 前缀可省）；无效值直接报错不出残缺报告")] = "/Game", target: Annotated[str | None, Field(description="pc|console|mobile；缺省读 prism.toml 否则 pc")] = None, project: Annotated[str | None, Field(description="工程选择器：名称/路径/slug；仅一个活跃工程时可省略")] = None, recent_tasks: Annotated[int, Field(description="cook 类规则只审计最近 N 个成功 cook/package 档案（陈旧证据自动过期）；0=全部历史供取证")] = rules.DEFAULT_RECENT_TASKS):
    """Static performance audit from the offline rule pack (v0.3 PR-A).

    Reads disk + cook task archives only; findings carry rule_id/severity/subject/
    evidence/threshold/advice, sorted error>warn, capped by total/truncated/cap.
    Bridge-channel rules join when the editor is live; offline yields a half report.

    scope 必须是 Content 下真实存在的文件夹（/Game 前缀可省略）；无效值直接返回 RUNTIME_ERROR，
    不再静默跳过规则产出"看似干净"的残缺报告。cook 类规则只审计最近 recent_tasks 个成功的
    cook/package 档案（默认 3；0=全部历史档案，供取证），陈旧证据随更新任务自动过期，
    窗口明细见返回的 cook_archive 字段。
    """
    bus_dir, pd, err = _resolve(project, allow_stale=True)
    if err:
        return err
    if not pd:
        return envelope.make_err(envelope.Code.RUNTIME_ERROR, "no project_dir (pass project= / --project-dir)")
    if not bus_dir:
        return envelope.make_err(envelope.Code.RUNTIME_ERROR, "no bus_dir for cook archives")
    try:
        return envelope.make_ok(rules.run_report(pd, bus_dir, scope=scope, target=target,
                                                 recent_tasks=recent_tasks))
    except ValueError as e:
        return envelope.make_err(envelope.Code.RUNTIME_ERROR, str(e))
    except Exception as e:
        return envelope.make_err(envelope.Code.RUNTIME_ERROR, str(e), traceback.format_exc())


def list_perf_rules(target: Annotated[str | None, Field(description="pc|console|mobile")] = None, project: Annotated[str | None, Field(description="工程选择器：名称/路径/slug；仅一个活跃工程时可省略")] = None):
    """List active rule pack (id/channel/thresholds/doc) for report explainability."""
    _b, pd, _err = _resolve(project, allow_stale=True)
    try:
        return envelope.make_ok(rules.list_rules(target, project_dir=(pd or "")))
    except ValueError as e:
        return envelope.make_err(envelope.Code.RUNTIME_ERROR, str(e))


def list_projects():
    """List registered UE projects and which have a live bridge (zero-config discovery).

    Discovery source is the per-user registry dir written by the in-editor bridge; liveness
    is derived from each project's bus heartbeat freshness. Call this to pick a `project=` value.
    """
    items = []
    for e in registry.discover():
        items.append({
            "name": e.get("name") or e.get("slug"),
            "project_dir": e.get("project_dir"),
            "bus_dir": e.get("bus_dir"),
            "ue_version": e.get("ue_version"),
            "live": bool(e.get("live")),
            "heartbeat_age": e.get("heartbeat_age"),
        })
    items.sort(key=lambda d: str(d.get("name") or ""))
    live = sum(1 for i in items if i["live"])
    return envelope.make_ok({
        "projects": items, "live": live,
        "explicit_config": _has_explicit(),
        "total": len(items), "truncated": False, "cap": len(items),
    })


# ---------------- 迁移套件（v1.0 · 预览只读 / 写操作双钥） ----------------

def _build_plan(client, ap, new_name, dest_path, include_impact=True, impact_limit=200):
    """共用计划构建：describe_asset(源) + 目标冲突(+ 可选 used_by 影响面) -> (plan, err)。
    err 非 None=源查询失败(透传桥错误)；plan.source_missing=True 表示源不存在。绝不改动任何东西。"""
    src = client.call("describe_asset", {"asset_path": ap})
    if not src.get("ok"):
        return None, src
    src_r = src["result"] or {}
    leaf = ap.rstrip("/").rsplit("/", 1)[-1]
    target = None
    if dest_path:
        target = str(dest_path).rstrip("/") + "/" + (new_name or leaf)
    elif new_name:
        target = ap.rstrip("/").rsplit("/", 1)[0] + "/" + new_name
    action = []
    if new_name:
        action.append("rename")
    if dest_path:
        action.append("move")
    conflict = False
    if target:
        tgt = client.call("describe_asset", {"asset_path": target})
        _tr = tgt.get("result") or {}
        # 目标若只是改名遗留的 ObjectRedirector 桩，允许覆盖（如把资产改名回原名），不算冲突
        conflict = bool(tgt.get("ok") and _tr.get("found")
                        and str(_tr.get("class") or "") != "ObjectRedirector")
    impact = None
    if include_impact:
        ref = client.call("get_asset_references",
                          {"asset_path": ap, "direction": "used_by", "recursive": True, "limit": int(impact_limit)})
        impact = {"ok": bool(ref.get("ok"))}
        if ref.get("ok"):
            rr = ref["result"] or {}
            impact.update({
                "referencer_count": rr.get("used_by_total", 0),
                "referencers": rr.get("used_by", []),
                "truncated": rr.get("truncated", False),
                "cap": rr.get("cap"),
            })
            if rr.get("note"):
                impact["note"] = rr["note"]
        else:
            impact["error"] = ref.get("error")
    plan = {
        "source": {"path": ap, "found": bool(src_r.get("found")), "class": src_r.get("class"),
                   "package": src_r.get("package_name"), "note": src_r.get("note")},
        "source_missing": not src_r.get("found"),
        "action": action, "target": target, "conflict": conflict, "impact": impact,
    }
    return plan, None

def preview_asset_migration(asset_path: Annotated[str, Field(description="/Game 包路径，如 /Game/Foo/Bar")], new_name: Annotated[str | None, Field(description="目标改名（可选）")] = None, dest_path: Annotated[str | None, Field(description="目标目录（可选，如 /Game/Moved）")] = None, project: Annotated[str | None, Field(description="工程选择器：名称/路径/slug；仅一个活跃工程时可省略")] = None, impact_limit: Annotated[int, Field(description="引用者清单上限")] = 200):
    """Dry-run 迁移预览（纯只读）：解析目标路径 + 冲突检测 + used_by 影响面，绝不改动任何东西。

    给 new_name -> 计划改名；给 dest_path -> 计划移动到该目录（可与 new_name 同时）。
    返回 plan(目标/冲突) + impact(引用者数量与清单)，供 agent/用户确认后再上写操作（PR-4）。
    需要桥在线（走 describe_asset / get_asset_references）。
    """
    bus_dir, _pd, err = _resolve(project)
    if err:
        return err
    ap = str(asset_path or "").strip()
    if not ap.startswith("/Game"):
        return envelope.make_err(envelope.Code.RUNTIME_ERROR, "asset_path 需是 /Game 包路径")
    if not new_name and not dest_path:
        return envelope.make_err(envelope.Code.RUNTIME_ERROR, "需给 new_name(改名) 或 dest_path(移动) 之一")

    client = bus.BusClient(bus_dir, timeout=bus.DEFAULT_TIMEOUT)
    plan, perr = _build_plan(client, ap, new_name, dest_path, include_impact=True, impact_limit=impact_limit)
    if perr:
        return perr
    if plan["source_missing"]:
        return envelope.make_err(envelope.Code.RUNTIME_ERROR,
                                 "源资产未找到: %s (%s)" % (ap, plan["source"].get("note")))
    return envelope.make_ok({
        "dry_run": True,
        "source": plan["source"], "action": plan["action"], "target": plan["target"],
        "conflict": plan["conflict"], "impact": plan["impact"],
        "note": "仅预览，未改动任何东西。conflict=True 时勿执行；真写 migrate_asset_rename/move 双钥（dry_run=False+confirm=True）。",
    })


def _migrate(op, asset_path, new_name, dest_path, dry_run, confirm, project, fixup_redirectors=True):
    bus_dir, _pd, err = _resolve(project)
    if err:
        return err
    try:
        dry_run = _as_bool(dry_run, default=True)
        confirm = _as_bool(confirm, default=False)
        fixup_redirectors = _as_bool(fixup_redirectors, default=True)
    except ValueError as e:
        return envelope.make_err(envelope.Code.RUNTIME_ERROR, str(e))
    ap = str(asset_path or "").strip()
    if not ap.startswith("/Game"):
        return envelope.make_err(envelope.Code.RUNTIME_ERROR, "asset_path 需是 /Game 包路径")
    if op == "rename" and not new_name:
        return envelope.make_err(envelope.Code.RUNTIME_ERROR, "改名需 new_name")
    if op == "move" and not dest_path:
        return envelope.make_err(envelope.Code.RUNTIME_ERROR, "移动需 dest_path")
    client = bus.BusClient(bus_dir, timeout=bus.DEFAULT_TIMEOUT)
    if op == "rename":
        nn, dp = new_name, None
    else:
        nn, dp = new_name, dest_path
    do_impact = bool(dry_run) or not confirm
    plan, perr = _build_plan(client, ap, nn, dp, include_impact=do_impact)
    if perr:
        return perr
    if plan["source_missing"]:
        return envelope.make_err(envelope.Code.RUNTIME_ERROR,
                                 "源资产未找到: %s (%s)" % (ap, plan["source"].get("note")))
    if plan["conflict"]:
        return envelope.make_err(envelope.Code.RUNTIME_ERROR, "目标已存在，拒绝: %s" % plan["target"])
    if dry_run or not confirm:
        return envelope.make_ok({
            "dry_run": True, "would_execute": op, "target": plan["target"],
            "source": plan["source"], "impact": plan.get("impact"),
            "note": "未执行。确认无误后带 dry_run=False 且 confirm=True 重跑以真正%s。" % ("改名" if op == "rename" else "移动"),
        })
    if op == "rename":
        return client.call("migrate_asset_rename", {"asset_path": ap, "new_name": nn, "confirm": True, "fixup_redirectors": fixup_redirectors})
    return client.call("migrate_asset_move", {"asset_path": ap, "dest_path": dp, "new_name": nn, "confirm": True, "fixup_redirectors": fixup_redirectors})


def migrate_asset_rename(asset_path: Annotated[str, Field(description="/Game 包路径，如 /Game/Foo/Bar")], new_name: Annotated[str, Field(description="新资产名（不含路径）")], dry_run: Annotated[bool, Field(description="默认 true=只回预览/计划，不落盘不启动")] = True, confirm: Annotated[bool, Field(description="真实执行需 dry_run=false 且 confirm=true 双钥（写操作契约）")] = False, project: Annotated[str | None, Field(description="工程选择器：名称/路径/slug；仅一个活跃工程时可省略")] = None, fixup_redirectors: Annotated[bool, Field(description="true=改名成功后清一次重定向桩")] = True):
    """真改名（写操作 · 双钥）。默认 dry_run 只回计划；dry_run=False 且 confirm=True 才落盘。
    UE 自动修引用（内存态，需保存）；成功后默认清一次重定向桩(fixup_redirectors)，可用 False 关闭。"""
    return _migrate("rename", asset_path, new_name, None, dry_run, confirm, project, fixup_redirectors)


def migrate_asset_move(asset_path: Annotated[str, Field(description="/Game 包路径，如 /Game/Foo/Bar")], dest_path: Annotated[str, Field(description="目标目录（/Game 前缀可省）")], new_name: Annotated[str | None, Field(description="移动同时改名（可选）")] = None, dry_run: Annotated[bool, Field(description="默认 true=只回预览/计划，不落盘不启动")] = True, confirm: Annotated[bool, Field(description="真实执行需 dry_run=false 且 confirm=true 双钥（写操作契约）")] = False, project: Annotated[str | None, Field(description="工程选择器：名称/路径/slug；仅一个活跃工程时可省略")] = None, fixup_redirectors: Annotated[bool, Field(description="true=移动成功后清一次重定向桩")] = True):
    """真移动（写操作 · 双钥）。默认 dry_run 只回计划；双钥满足才落盘。可带 new_name 同时改名。
    成功后默认清一次重定向桩(fixup_redirectors)，可用 False 关闭。"""
    return _migrate("move", asset_path, new_name, dest_path, dry_run, confirm, project, fixup_redirectors)


def migrate_asset(asset_path: Annotated[str, Field(description="/Game 包路径，如 /Game/Foo/Bar")], dest_path: Annotated[str, Field(description="目标目录（镜像结构复制，原件不动）")], new_name: Annotated[str | None, Field(description="副本改名（可选）")] = None, dry_run: Annotated[bool, Field(description="默认 true=只回预览/计划，不落盘不启动")] = True, confirm: Annotated[bool, Field(description="真实执行需 dry_run=false 且 confirm=true 双钥（写操作契约）")] = False, project: Annotated[str | None, Field(description="工程选择器：名称/路径/slug；仅一个活跃工程时可省略")] = None):
    """迁移(A·同工程复制)：资产+其 /Game uses 闭包按镜像结构复制到 dest 目录。默认 dry_run 只回复制计划；
    双钥(dry_run=False+confirm=True)才落盘。原件不动。Needs live bridge."""
    bus_dir, _pd, err = _resolve(project)
    if err:
        return err
    try:
        dry_run = _as_bool(dry_run, default=True)
        confirm = _as_bool(confirm, default=False)
    except ValueError as e:
        return envelope.make_err(envelope.Code.RUNTIME_ERROR, str(e))
    return bus.BusClient(bus_dir, timeout=bus.DEFAULT_TIMEOUT).call(
        "migrate_asset",
        {"asset_path": asset_path, "dest_path": dest_path, "new_name": new_name, "dry_run": dry_run, "confirm": confirm})


_TOOLS = (
    ping, list_level_actors, describe_asset, get_asset_references, get_asset_metrics,
    get_asset_chain, scan_orphan_assets,
    read_editor_log, scan_folder_assets,
    cook_package, get_cook_status, attribute_cook_errors,
    get_perf_report, list_perf_rules,
    list_projects, preview_asset_migration, migrate_asset_rename, migrate_asset_move, migrate_asset,
)


def _make_server():
    try:
        from mcp.server import MCPServer  # SDK 2.x（首选）
    except Exception:
        try:
            from mcp.server.mcpserver import MCPServer
        except Exception:
            from mcp.server.fastmcp import FastMCP as MCPServer  # 旧版兜底
    return MCPServer("ue-prism")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="prism.server", description="ue-prism MCP server")
    ap.add_argument("--project-dir", help="UE 工程根（含 Saved/、Content/）")
    ap.add_argument("--bus-dir", help="文件总线目录（默认 <project-dir>/Saved/Prism）")
    ap.add_argument("--transport", default="stdio", choices=["stdio", "http", "streamable-http", "sse"])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args(argv)

    if args.project_dir:
        _CFG["project_dir"] = os.path.abspath(args.project_dir)
        os.environ["PRISM_PROJECT_DIR"] = _CFG["project_dir"]
    if args.bus_dir:
        _CFG["bus_dir"] = os.path.abspath(args.bus_dir)
        os.environ["PRISM_BUS_DIR"] = _CFG["bus_dir"]

    server = _make_server()
    for tool in _TOOLS:
        server.tool()(tool)

    # SDK 2.x run() 只接受 stdio|sse|streamable-http；把 http 归一到 streamable-http。
    transport = "streamable-http" if args.transport == "http" else args.transport
    if transport == "stdio":
        server.run(transport="stdio")
    else:
        server.run(transport=transport, host=args.host, port=args.port)


if __name__ == "__main__":
    main()