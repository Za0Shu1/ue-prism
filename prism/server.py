"""MCP server：把工具暴露给 MCP 客户端。server 侧不碰引擎 API（见 REQUIREMENTS §2）。

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
    点名命中但工程目录已不在盘上（注册表残留）-> PROJECT_NOT_FOUND 并回显缺失路径。
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
        if not entry.get("dir_exists"):
            # 注册表残留（工程被删/整目录搬走）：别说“没见过”，要说“指针过期”
            return None, None, envelope.make_err(
                envelope.Code.PROJECT_NOT_FOUND,
                "project %r is registered but its directory is gone: %s "
                "(stale registry pointer - reopen or re-register that project; "
                "see list_projects result.missing)"
                % (project, entry.get("project_dir")),
            )
        return entry["bus_dir"], entry["project_dir"], None
    if _has_explicit():
        b, p = _explicit()
        return b, p, None
    pool = registry.selectable() if allow_stale else registry.live_selectable()
    if len(pool) == 1:
        return pool[0]["bus_dir"], pool[0]["project_dir"], None
    if len(pool) == 0:
        if not allow_stale and registry.selectable():
            return None, None, envelope.make_err(
                envelope.Code.NO_PROJECT,
                "registered project(s) %s are offline (stale heartbeat); open the editor"
                % registry.names(registry.selectable()),
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


def list_level_actors(class_contains: Annotated[str | None, Field(description="按 Actor 类名子串过滤（不区分大小写）")] = None, tag: Annotated[str | None, Field(description="按 Actor tag 过滤")] = None, limit: Annotated[int, Field(description="最多返回的 Actor 数（截断带 truncated 标记）")] = 200, compose: Annotated[bool, Field(description="true=追加场景构成诊断（按类/子关卡计数 + HISM/ISM 合批机会清单）；大关卡会全量扫描组件，稍慢")] = False, dup_min_count: Annotated[int, Field(description="合批机会阈值：同一网格被 >= 此数的普通 StaticMeshActor 平铺放置")] = 5, composition_cap: Annotated[int, Field(description="构成摘要各组（类/关卡/机会清单）条目上限")] = 30, project: Annotated[str | None, Field(description="工程选择器：名称/路径/slug；仅一个活跃工程时可省略")] = None):
    """List actors in the loaded level (name/class/label/path/location/components). Needs the live bridge.

    compose=True 时另返 composition：按类/子关卡(streaming)聚合计数、
    已实例化(ISM/HISM)占比、以及"重复 StaticMesh 未实例化"合批机会清单
    （mesh + actor 数，降序截断）——drawcall/HISM 优化的决策输入。
    """
    bus_dir, _pd, err = _resolve(project)
    if err:
        return err
    return bus.BusClient(bus_dir, timeout=bus.DEFAULT_TIMEOUT).call(
        "list_level_actors",
        {"class_contains": class_contains, "tag": tag, "limit": limit,
         "compose": bool(compose), "dup_min_count": int(dup_min_count),
         "composition_cap": int(composition_cap)},
    )


def describe_asset(asset_path: Annotated[str, Field(description="/Game 包路径，如 /Game/Foo/Bar")], project: Annotated[str | None, Field(description="工程选择器：名称/路径/slug；仅一个活跃工程时可省略")] = None):
    """Read one asset's registry metadata (class/package/on-disk size). Needs the live bridge."""
    bus_dir, _pd, err = _resolve(project)
    if err:
        return err
    return bus.BusClient(bus_dir, timeout=bus.DEFAULT_TIMEOUT).call(
        "describe_asset", {"asset_path": asset_path})


def get_asset_references(asset_path: Annotated[str, Field(description="/Game 包路径，如 /Game/Foo/Bar")], direction: Annotated[str, Field(description="uses=依赖它的 | used_by=引用它的 | both=双向")] = "both", recursive: Annotated[bool, Field(description="true=递归展开依赖闭包（深度/节点数有上限）")] = False, limit: Annotated[int, Field(description="每方向返回条数上限")] = 500, classify_soft: Annotated[bool, Field(description="true=额外用 hard-only/soft-only 两次调用给 *_detail 标注每个引用的硬/软来源（默认关，行为不变）")] = False, project: Annotated[str | None, Field(description="工程选择器：名称/路径/slug；仅一个活跃工程时可省略")] = None):
    """Reference graph via AssetRegistry: uses(依赖)/used_by(被引用). direction in both|uses|used_by. classify_soft 可加标每个引用的硬/软来源. Needs live bridge."""
    bus_dir, _pd, err = _resolve(project)
    if err:
        return err
    return bus.BusClient(bus_dir, timeout=bus.DEFAULT_TIMEOUT).call(
        "get_asset_references",
        {"asset_path": asset_path, "direction": direction, "recursive": recursive,
         "limit": limit, "classify_soft": _as_bool(classify_soft, default=False)},
    )


def get_asset_chain(asset_path: Annotated[str, Field(description="/Game 包路径，如 /Game/Foo/Bar")], direction: Annotated[str, Field(description="uses（要带走的依赖链）| used_by（连累面）")] = "uses", scope: Annotated[str, Field(description="game=只含 /Game（默认）| all=含引擎资产")] = "game", with_meta: Annotated[bool, Field(description="true=逐节点补类名/磁盘大小并按类别聚合")] = True, max_nodes: Annotated[int, Field(description="闭包节点总数上限")] = 2000, max_depth: Annotated[int, Field(description="0=不限深度；>0 限制递归层数")] = 0, god_min_refs: Annotated[int, Field(description="uses 闭包共享枢纽阈值：/Game 引用者 >= 此值标 god_asset；0=关闭（仅 uses 方向生效）")] = 30, project: Annotated[str | None, Field(description="工程选择器：名称/路径/slug；仅一个活跃工程时可省略")] = None):
    """递归依赖闭包（迁移预览）：uses=该资产依赖的整条链(迁移要带走)；used_by=谁依赖它(连累面)。
    scope=game(默认)只留 /Game；with_meta 给每节点补 class+size_bytes 并按类别聚合体量。
    加固：cycles=闭包内耦合环(不可拆分子集迁移)；god_assets=uses 链上的共享枢纽(used_by_game>=阈值)；impact_summary=used_by 影响半径摘要(波及关卡/按类体量)。Needs live bridge."""
    bus_dir, _pd, err = _resolve(project)
    if err:
        return err
    return bus.BusClient(bus_dir, timeout=bus.DEFAULT_TIMEOUT).call(
        "get_asset_chain",
        {"asset_path": asset_path, "direction": direction, "scope": scope, "with_meta": with_meta,
         "max_nodes": max_nodes, "max_depth": max_depth, "god_min_refs": god_min_refs})


def _as_path_list(value):
    """Normalize asset_paths: list/tuple as-is, or a string split on , / ; / newline."""
    if not value:
        return []
    if isinstance(value, (list, tuple)):
        seq = list(value)
    else:
        seq = re.split(r"[,\n;]+", str(value))
    return [t for t in (str(x).strip() for x in seq) if t]


def get_asset_metrics(asset_paths: Annotated[str, Field(description="多个 /Game 路径，支持逗号/分号/换行分隔")], max_assets: Annotated[int, Field(description="本次最多载入编辑器内存的资产数（保持小值）")] = 20, probe_source: Annotated[bool, Field(description="是否调引擎源尺寸探针（默认 true=探，source_width 取源侧真值）。批量写后紧贴着重扫可置 false，规避 5.4 已知引擎断言竞态（贴图重压缩中调 blueprint_get_texture_source_disk_and_memory_size 会崩编辑器）；关掉后 sampling.source_probe=off、判定退化为运行时边长")] = True, project: Annotated[str | None, Field(description="工程选择器：名称/路径/slug；仅一个活跃工程时可省略")] = None):
    """Batch raw asset metrics via bridge (texture px / mesh LOD triangles), with tried/api evidence.

    每项附源盘大小(disk_bytes/disk_mb)与真大/假大判定：runtime_verdict
    (runtime_heavy=真显存/帧大户 | disk_only_bloat=源盘膨胀运行时被限死 | normal | unknown)
    与 is_runtime_heavy；纹理另采 compression/virtual_texture/max_size/array_size，
    以及源边长可信度 source_side_exact / size_basis（false=反推多解，池收益会被安全阀撤回）。
    立方图另回传 source_layout：cube_array=阵列源 6xS^2 / panorama_2to1=2:1 全景源 2xS^2
    （#11 D 双假设，POOL_MODEL v4）——全景类按面数反推会把面边长推小、运行时低估 3 倍。
    Loads assets into editor memory as a side effect - keep max_assets small. Needs live bridge.
    """
    bus_dir, _pd, err = _resolve(project)
    if err:
        return err
    return bus.BusClient(bus_dir, timeout=bus.DEFAULT_TIMEOUT).call(
        "get_asset_metrics", {"asset_paths": _as_path_list(asset_paths), "max_assets": int(max_assets),
         "probe_source": _as_bool(probe_source, True)})


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


def scan_broken_references(asset_paths: Annotated[str, Field(description="只查这些资产（逗号/分号/换行分隔的 /Game 路径）；填了则忽略 folder")] = "", folder: Annotated[str, Field(description="扫描范围，Content 下目录前缀（默认 /Game）")] = "/Game", limit: Annotated[int, Field(description="本窗口最多扫描多少资产（逐个查硬依赖）")] = 200, offset: Annotated[int, Field(description="分页偏移")] = 0, max_items: Annotated[int, Field(description="broken/by_dep 明细输出上限")] = 200, include_engine: Annotated[bool, Field(description="true=把 /Engine 依赖也纳入（默认只看 /Game）")] = False, project: Annotated[str | None, Field(description="工程选择器：名称/路径/slug；仅一个活跃工程时可省略")] = None):
    """扫描悬空/坏引用（只读）：资产硬依赖指向的 /Game 包无法解析——missing(目标已不在注册表)或 redirector(只剩重定向桩，可修复)。Needs live bridge.

    kind=missing 通常需从 VCS 还原或重新指向；kind=redirector 可由 fix_broken_references 安全修复
    （fixable_redirector_deps 给其输入）。静态口径不含软引用/运行时拼字符串路径。工程越大用 limit/offset 分页。
    """
    bus_dir, _pd, err = _resolve(project)
    if err:
        return err
    paths = [x.strip() for x in re.split(r"[,;\n]+", asset_paths or "") if x.strip()]
    return bus.BusClient(bus_dir, timeout=bus.DEFAULT_TIMEOUT).call(
        "scan_broken_references",
        {"asset_paths": paths, "folder": folder, "limit": int(limit), "offset": int(offset),
         "max_items": int(max_items), "include_engine": _as_bool(include_engine, default=False)})

def scan_missing_source_files(folder: Annotated[str, Field(description="扫描范围，Content 下目录前缀（默认 /Game）")] = "/Game", class_contains: Annotated[str, Field(description="按类名子串过滤（不区分大小写），如 Texture；留空=全部资产")] = "", limit: Annotated[int, Field(description="本窗口最多检查多少个资产（每个都会 load_asset 进编辑器内存，代价高）")] = 200, offset: Annotated[int, Field(description="分页偏移（按 uasset 体量降序窗口）")] = 0, max_items: Annotated[int, Field(description="rows 明细输出上限")] = 200, project: Annotated[str | None, Field(description="工程选择器：名称/路径/slug；仅一个活跃工程时可省略")] = None):
    """诊断导入原文件已不在记录路径上的资产（只读）：这类资产改尺寸/改压缩/重导入都无从下手，只能引擎侧钳尺寸。Needs live bridge.

    读 asset_import_data 定格的原始文件绝对路径并判本机在场性，输出 present/partial/missing/
    no_record/load_failed 五档 + 按资产目录聚合。诚实边界：记录路径属导入那一刻（可能别人机器/
    别盘符），missing 不等于美术没留档，**不可据此删资产**；missing_mb 是受影响 uasset 体量（可维护性口径），
    不是显存/池金额。载入有编辑器内存副作用，先用 class_contains 缩面再翻页。
    """
    bus_dir, _pd, err = _resolve(project)
    if err:
        return err
    return bus.BusClient(bus_dir, timeout=240).call(
        "scan_missing_source_files",
        {"folder": folder, "class_contains": class_contains, "limit": int(limit),
         "offset": int(offset), "max_items": int(max_items)})


# ---------------- 离线工具（读磁盘） ----------------

def read_editor_log(level: Annotated[str, Field(description="Error|Warning|Display|All")] = "Error", tail: Annotated[int, Field(description="从日志末尾读取的行数")] = 2000, top: Annotated[int, Field(description="聚合分组上限")] = 30, project_dir: Annotated[str | None, Field(description="显式工程目录（与 project= 二选一）")] = None, project: Annotated[str | None, Field(description="工程选择器：名称/路径/slug；仅一个活跃工程时可省略")] = None):
    """Aggregate editor log lines by normalized fingerprint (category/count/first_seen/sample). Offline; no bridge needed.

    P2 指纹归一：路径/引号串/十六进制地址/数字抹平成 <path>/<addr>/#，把"同模板不同资产/帧号"的
    噪音行合并成一个组（真实大工程实测 Warning distinct 194->10）。每组从原文登记 assets（/Game、
    /Engine、.uasset/.umap 引用）与 asset_count/assets_truncated，支持 warning 归因到具体资产。
    """
    pd, err = _resolve_offline_dir(project_dir, project)
    if err:
        return err
    try:
        return envelope.make_ok(logscan.read_editor_log(pd, level=level, tail=int(tail), top=int(top)))
    except FileNotFoundError as e:
        return envelope.make_err(envelope.Code.RUNTIME_ERROR, str(e))
    except Exception as e:
        return envelope.make_err(envelope.Code.RUNTIME_ERROR, str(e), traceback.format_exc())


def scan_folder_assets(folder: Annotated[str, Field(description="Content 下文件夹，/Game 前缀可省略")] = "/Game", sort: Annotated[str, Field(description="size（默认）| path")] = "size", limit: Annotated[int, Field(description="返回条数上限")] = 500, budget_depth: Annotated[int, Field(description="目录预算聚合深度：1=一级子目录（默认）；0=叶子目录全链")] = 1, budget_cap: Annotated[int, Field(description="目录预算返回条数上限；<=0 全返")] = 30, project_dir: Annotated[str | None, Field(description="显式工程目录（与 project= 二选一）")] = None, project: Annotated[str | None, Field(description="工程选择器：名称/路径/slug；仅一个活跃工程时可省略")] = None):
    """Audit on-disk assets under <project>/Content: sorted by size + per-type + per-directory budget. Offline; no bridge."""
    pd, err = _resolve_offline_dir(project_dir, project)
    if err:
        return err
    try:
        return envelope.make_ok(folderscan.scan_folder_assets(pd, folder=folder, sort=sort, limit=int(limit), budget_depth=int(budget_depth), budget_cap=int(budget_cap)))
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

def get_perf_report(scope: Annotated[str, Field(description="必须是 Content 下真实存在的文件夹（/Game 前缀可省）；无效值直接报错不出残缺报告")] = "/Game", target: Annotated[str | None, Field(description="pc|console|mobile；缺省读 prism.toml 否则 pc")] = None, project: Annotated[str | None, Field(description="工程选择器：名称/路径/slug；仅一个活跃工程时可省略")] = None, recent_tasks: Annotated[int, Field(description="cook 类规则只审计最近 N 个成功 cook/package 档案（陈旧证据自动过期）；0=全部历史供取证")] = rules.DEFAULT_RECENT_TASKS, cap: Annotated[int, Field(description="返回 finding 上限（error 优先已排序）；<=0=全部返回")] = rules.DEFAULT_CAP, sample_size: Annotated[int, Field(description="bridge 规则度量最大的前 N 个大资产（0=扫描宇宙全量，慎用内存）；越大覆盖越全越慢")] = rules.DEFAULT_SAMPLE, snapshot: Annotated[bool, Field(description="true(默认)=本次报告落一份快照到 <bus>/reports/ 并与上次比较返回 diff 回归对比；false=只出不写快照")] = True):
    """Static performance audit from the offline rule pack (v0.3 PR-A).

    Reads disk + cook task archives only; findings carry rule_id/severity/subject/
    evidence/threshold/advice + roi(unit/benefit/effort/rationale)，sorted error>warn, capped
    by total/truncated/cap；顶层 roi_summary 汇总体量收益(MB)/按成本分档/正确性计数，供排优先级。
    snapshot=True 时返回 diff(vs 上次快照的新增/移除/严重度变化 + error/warn/体量MB 增量)；首跑无基线 diff.available=False。
    Bridge-channel rules join when the editor is live; offline yields a half report.

    采样可控：bridge 规则默认只度量最大的 80 个资产（sample_size=80），在万级资产工程上
    会系统性欠采样。sample_size 调大度量面（0=扫描宇宙全量，编辑器内存开销大、慎用），
    或缩窄 scope 做定向深扫；cap 控制返回 finding 条数（<=0 全返）。返回体含 sampling
    (sample_size/scan_universe/measured/exhaustive) 与 errors_hidden（被 cap 截掉的 error 数）。

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
                                                 recent_tasks=recent_tasks, cap=cap,
                                                 sample_size=sample_size,
                                                 snapshot=_as_bool(snapshot, default=True)))
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

    `projects` only lists pointers whose project_dir still exists (those are the selectable
    ones); leftover pointers for deleted/moved projects are reported in `missing` (+
    `missing_count`) instead of being silently dropped or offered as targets.
    """
    items, missing = [], []
    for e in registry.discover():
        row = {
            "name": e.get("name") or e.get("slug"),
            "project_dir": e.get("project_dir"),
            "bus_dir": e.get("bus_dir"),
            "ue_version": e.get("ue_version"),
            "live": bool(e.get("live")),
            "heartbeat_age": e.get("heartbeat_age"),
        }
        (items if e.get("dir_exists") else missing).append(row)
    key = lambda d: str(d.get("name") or "")
    items.sort(key=key)
    missing.sort(key=key)
    live = sum(1 for i in items if i["live"])
    return envelope.make_ok({
        "projects": items, "live": live,
        "missing": missing, "missing_count": len(missing),
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


def fix_broken_references(folder: Annotated[str, Field(description="扫描/修复范围，Content 下目录前缀（默认 /Game）")] = "/Game", asset_paths: Annotated[str, Field(description="只修这些资产的坏引用（逗号/分号/换行分隔；填了则忽略 folder）")] = "", dry_run: Annotated[bool, Field(description="默认 true=只回修复计划，不落盘")] = True, confirm: Annotated[bool, Field(description="真实执行需 dry_run=False 且 confirm=True 双钥（写操作契约）")] = False, limit: Annotated[int, Field(description="本窗口最多扫描多少资产")] = 200, project: Annotated[str | None, Field(description="工程选择器：名称/路径/slug；仅一个活跃工程时可省略")] = None):
    """修复悬空引用中『可安全自动修复』的子集：目标只剩重定向桩(redirector)者调 fix_up_redirectors 把引用者直接指向目标；
    missing(目标已彻底不在)只列出不碰。默认 dry_run 出计划，双钥(dry_run=False+confirm=True)才落盘。Needs live bridge.
    注：5.4 python 若未绑定 fix_up_redirectors，会诚实报 attempted 但未确认，需编辑器手动兜底，不假装成功。"""
    bus_dir, _pd, err = _resolve(project)
    if err:
        return err
    try:
        dry_run = _as_bool(dry_run, default=True)
        confirm = _as_bool(confirm, default=False)
    except ValueError as e:
        return envelope.make_err(envelope.Code.RUNTIME_ERROR, str(e))
    paths = [x.strip() for x in re.split(r"[,;\n]+", asset_paths or "") if x.strip()]
    return bus.BusClient(bus_dir, timeout=bus.DEFAULT_TIMEOUT).call(
        "fix_broken_references",
        {"folder": folder, "asset_paths": paths, "dry_run": dry_run,
         "confirm": confirm, "limit": int(limit)})


# ---------------- v1.5 优化族（纹理流送池 / Nanite / 合批；读诊断 + opt_ 双钥写入）----------------

def _bool_opt(name, value, default):
    """MCP 通道把 bool 送成字符串：统一走 _as_bool，异常转结构化错误（双钥契约不靠运气）。"""
    try:
        return _as_bool(value, default=default), None
    except ValueError as exc:
        return None, envelope.make_err(envelope.Code.RUNTIME_ERROR, str(exc))


def scan_texture_streaming(folder: Annotated[str, Field(description="Content 下目录前缀（默认 /Game）")] = "/Game", map: Annotated[str | None, Field(description="改按关卡 uses 闭包取纹理（如 /Game/Main_City）")] = None, paths: Annotated[str | None, Field(description="只查这些纹理（逗号/分号/换行分隔；给了就忽略 folder/map）")] = None, max_assets: Annotated[int, Field(description="本窗口最多度量多少张（按磁盘体量降序）；载入编辑器内存有代价")] = 200, min_mb: Annotated[float, Field(description="单张运行时估算低于此 MB 且无收益则不列进 items（仍计入 summary）")] = 4.0, top: Annotated[int, Field(description="items 返回条数上限")] = 25, offset: Annotated[int, Field(description="分页偏移（按体量降序窗口）")] = 0, probe_source: Annotated[bool, Field(description="是否调引擎源尺寸探针（默认 true=探，source_width 取源侧真值）。批量写后紧贴着重扫可置 false，规避 5.4 已知引擎断言竞态（贴图重压缩中调 blueprint_get_texture_source_disk_and_memory_size 会崩编辑器）；关掉后 sampling.source_probe=off、判定退化为运行时边长")] = True, project: Annotated[str | None, Field(description="工程选择器：名称/路径/slug；仅一个活跃工程时可省略")] = None):
    """纹理流送池只读诊断（v1.5 Feature 1a）。Needs live bridge.

    按 folder / 关卡闭包 / 显式 paths 枚举纹理，逐张给全链运行时估算并归入
    池内 / 池外常驻(never_stream) / 已虚拟化，汇出 pool_demand_mb 与三档收益
    （钳尺寸 / 开 VT / 改压缩），并读工程 ini 的 *PoolSize 给 overflow 判定。
    口径与 get_perf_report 的 oversized_mip / uncompressed_texture /
    texture_streaming_overflow 同源（prism/streaming.py），两套账不会打架。
    安全阀：源边长反推未通过校验的条目 flags 含 size_unverified（size_basis=source_unverified），
    金额字段是 None 而非 0.0，但发现/clamp_to/actions 照给；summary.size_unverified_* 给不确定量。
    """
    bus_dir, _pd, err = _resolve(project)
    if err:
        return err
    return bus.BusClient(bus_dir, timeout=240).call(
        "scan_texture_streaming",
        {"folder": folder, "map": map, "paths": paths, "max_assets": int(max_assets),
         "min_mb": float(min_mb), "top": int(top), "offset": int(offset),
         "probe_source": _as_bool(probe_source, True)})


def opt_texture_set_virtual(paths: Annotated[str | None, Field(description="要改的纹理（逗号/分号/换行分隔）；给了则忽略 folder/map")] = None, folder: Annotated[str, Field(description="批量范围：Content 下目录前缀（默认 /Game）")] = "/Game", map: Annotated[str | None, Field(description="改按关卡 uses 闭包选纹理")] = None, limit: Annotated[int, Field(description="本窗口最多处理多少张（按体量降序）")] = 200, dry_run: Annotated[bool, Field(description="默认 true=只回计划（每张 before→after + 预计省多少 MB），不写盘")] = True, confirm: Annotated[bool, Field(description="真实执行需 dry_run=False 且 confirm=True 双钥（写操作契约）")] = False, probe_source: Annotated[bool, Field(description="false=跳过引擎源尺寸探针（默认 true=探）。批量写后紧贴着重扫请置 false，规避 5.4 已知引擎断言竞态（贴图重压缩中调 blueprint_get_texture_source_disk_and_memory_size 会崩编辑器）")] = True, list_cap: Annotated[int, Field(description="返回的 targets 行数上限（默认 80；0=不截断）。计划行按 change 优先排序，宽窗口核对候选时置 0 可拿全量")] = 80, project: Annotated[str | None, Field(description="工程选择器：名称/路径/slug；仅一个活跃工程时可省略")] = None):
    """批量开虚拟纹理（写·双钥；v1.5 Feature 1b）。Needs live bridge.

    Cube/Volume 不支持 VT -> skipped；已虚拟 -> no_op；属性写不进 -> failed + tried。
    工程需启用 VT（返回体 vt_enabled_project 给 ini 判定，None=没读到）。改完需保存，
    虚拟化数据由编辑器/ cook 生成；复跑 scan_texture_streaming 核对池需求下降。
    """
    bus_dir, _pd, err = _resolve(project)
    if err:
        return err
    dry_run, e1 = _bool_opt("dry_run", dry_run, True)
    confirm, e2 = _bool_opt("confirm", confirm, False)
    if e1 or e2:
        return e1 or e2
    return bus.BusClient(bus_dir, timeout=300).call(
        "opt_texture_set_virtual",
        {"paths": paths, "folder": folder, "map": map, "limit": int(limit),
         "dry_run": dry_run, "confirm": confirm,
         "probe_source": _as_bool(probe_source, True),
         "list_cap": int(list_cap)})


def opt_texture_clamp_max_size(paths: Annotated[str | None, Field(description="要改的纹理；给了则忽略 folder/map")] = None, folder: Annotated[str, Field(description="批量范围：Content 下目录前缀（默认 /Game）")] = "/Game", map: Annotated[str | None, Field(description="改按关卡 uses 闭包选纹理")] = None, max_size: Annotated[int, Field(description="钳到的分辨率（0=取消限制，属放大动作，会显著提示）")] = 2048, only_if_larger: Annotated[bool, Field(description="默认 true=只对大于 max_size 的生效")] = True, limit: Annotated[int, Field(description="本窗口最多处理多少张")] = 200, dry_run: Annotated[bool, Field(description="默认 true=只回计划")] = True, confirm: Annotated[bool, Field(description="真实执行需 dry_run=False 且 confirm=True 双钥")] = False, probe_source: Annotated[bool, Field(description="false=跳过引擎源尺寸探针（默认 true=探）。批量写后紧贴着重扫请置 false，规避 5.4 已知引擎断言竞态（贴图重压缩中调 blueprint_get_texture_source_disk_and_memory_size 会崩编辑器）")] = True, list_cap: Annotated[int, Field(description="返回的 targets 行数上限（默认 80；0=不截断）。计划行按 change 优先排序，宽窗口核对候选时置 0 可拿全量")] = 80, project: Annotated[str | None, Field(description="工程选择器：名称/路径/slug；仅一个活跃工程时可省略")] = None):
    """批量钳 MaxTextureSize（写·双钥；v1.5 Feature 1b）。Needs live bridge.

    降流送池压力最便宜的一刀：只改属性，不动源图。dry-run 给每张 before→after
    与 savings_mb；已达标/已在阈值内 -> no_op。改动需编辑器重压缩才反映到盘上。
    安全阀：源边长是反推估计的行 savings_mb=None + size_unverified（计划照给、写入不受影响），
    顶层 savings_withheld 计数并在 note 播报——别把没把握的钱当成省下来的钱。
    """
    bus_dir, _pd, err = _resolve(project)
    if err:
        return err
    dry_run, e1 = _bool_opt("dry_run", dry_run, True)
    confirm, e2 = _bool_opt("confirm", confirm, False)
    only_if_larger, e3 = _bool_opt("only_if_larger", only_if_larger, True)
    if e1 or e2 or e3:
        return e1 or e2 or e3
    return bus.BusClient(bus_dir, timeout=300).call(
        "opt_texture_clamp_max_size",
        {"paths": paths, "folder": folder, "map": map, "max_size": int(max_size),
         "only_if_larger": only_if_larger, "limit": int(limit),
         "dry_run": dry_run, "confirm": confirm,
         "probe_source": _as_bool(probe_source, True),
         "list_cap": int(list_cap)})


def opt_texture_set_compression(paths: Annotated[str | None, Field(description="要改的纹理；给了则忽略 folder/map")] = None, folder: Annotated[str, Field(description="批量范围（默认 /Game）")] = "/Game", map: Annotated[str | None, Field(description="改按关卡 uses 闭包选纹理")] = None, compression: Annotated[str, Field(description="目标格式友好名：default/normal/masks/editoricon（大小写与下划线不敏感）")] = "default", only_uncompressed: Annotated[bool, Field(description="默认 true=只动非工程运行时格式（真未压缩系 / EditorIcon 这类禁流送系）；格式文本判不准也不动")] = True, limit: Annotated[int, Field(description="本窗口最多处理多少张")] = 200, dry_run: Annotated[bool, Field(description="默认 true=只回计划")] = True, confirm: Annotated[bool, Field(description="真实执行需 dry_run=False 且 confirm=True 双钥")] = False, probe_source: Annotated[bool, Field(description="false=跳过引擎源尺寸探针（默认 true=探）。批量写后紧贴着重扫请置 false，规避 5.4 已知引擎断言竞态（贴图重压缩中调 blueprint_get_texture_source_disk_and_memory_size 会崩编辑器）")] = True, list_cap: Annotated[int, Field(description="返回的 targets 行数上限（默认 80；0=不截断）。计划行按 change 优先排序，宽窗口核对候选时置 0 可拿全量")] = 80, project: Annotated[str | None, Field(description="工程选择器：名称/路径/slug；仅一个活跃工程时可省略")] = None):
    """批量改压缩格式（写·双钥；v1.5 Feature 1b 可选项）。Needs live bridge.

    两类非工程运行时格式，收益口径不同（结果里分行给 savings_mb）：
      * 未压缩/浮点系：按 4B/px 计，改 TC_Default 约省 3/4 池；
      * TC_EDITOR_ICON / TC_MAX：无 mip 且禁用流送，整张恒常驻池外——改回 TC_Default
        是"变可流送、释放常驻"，不是省池，开 VT 对它无从谈起。
    默认只动这两类（only_uncompressed），格式文本判不准(unknown)一律不动，避免误伤
    美术贴图；确认要强改传 only_uncompressed=False。改完需重压缩（保存后编辑器/下次 cook）。
    """
    bus_dir, _pd, err = _resolve(project)
    if err:
        return err
    dry_run, e1 = _bool_opt("dry_run", dry_run, True)
    confirm, e2 = _bool_opt("confirm", confirm, False)
    only_uncompressed, e3 = _bool_opt("only_uncompressed", only_uncompressed, True)
    if e1 or e2 or e3:
        return e1 or e2 or e3
    return bus.BusClient(bus_dir, timeout=300).call(
        "opt_texture_set_compression",
        {"paths": paths, "folder": folder, "map": map, "compression": str(compression),
         "only_uncompressed": only_uncompressed, "limit": int(limit),
         "dry_run": dry_run, "confirm": confirm,
         "probe_source": _as_bool(probe_source, True),
         "list_cap": int(list_cap)})


def scan_nanite_candidates(folder: Annotated[str, Field(description="Content 下目录前缀（默认 /Game）")] = "/Game", map: Annotated[str | None, Field(description="改按关卡 uses 闭包取网格")] = None, paths: Annotated[str | None, Field(description="只查这些网格")] = None, min_triangles: Annotated[int, Field(description="低于此 LOD0 三角数算 low_complexity（默认 20 万，同 rules.mesh_tri warn）")] = 200000, max_assets: Annotated[int, Field(description="本窗口最多度量多少个网格")] = 200, top: Annotated[int, Field(description="items 返回条数上限")] = 40, offset: Annotated[int, Field(description="分页偏移")] = 0, project: Annotated[str | None, Field(description="工程选择器：名称/路径/slug；仅一个活跃工程时可省略")] = None):
    """Nanite 只读候选与冲突分析（v1.5 Feature 2a）。Needs live bridge.

    分类：enable_candidate（高面数未开，材质不半透明）/ conflict_translucent
    （已开却挂半透明 = 静默降级 + 刷警告）/ blocked_translucent（半透明不建议开）/
    already_ok / low_complexity / unknown_api（跨版本读不到开关）。
    附带汇报需补 bUsedWithNanite 的材质（本工具不改材质）。
    """
    bus_dir, _pd, err = _resolve(project)
    if err:
        return err
    return bus.BusClient(bus_dir, timeout=240).call(
        "scan_nanite_candidates",
        {"folder": folder, "map": map, "paths": paths, "min_triangles": int(min_triangles),
         "max_assets": int(max_assets), "top": int(top), "offset": int(offset)})


def opt_mesh_set_nanite(paths: Annotated[str | None, Field(description="要改的网格；给了则忽略 folder/map")] = None, folder: Annotated[str, Field(description="批量范围（默认 /Game）")] = "/Game", map: Annotated[str | None, Field(description="改按关卡 uses 闭包选网格")] = None, enabled: Annotated[bool, Field(description="true=开 Nanite，false=关")] = True, skip_translucent: Annotated[bool, Field(description="默认 true=挂半透明材质的网格不开（开了也静默降级+刷警告）")] = True, limit: Annotated[int, Field(description="本窗口最多处理多少个网格")] = 200, dry_run: Annotated[bool, Field(description="默认 true=只回计划")] = True, confirm: Annotated[bool, Field(description="真实执行需 dry_run=False 且 confirm=True 双钥")] = False, project: Annotated[str | None, Field(description="工程选择器：名称/路径/slug；仅一个活跃工程时可省略")] = None):
    """批量开/关 Nanite（写·双钥；v1.5 Feature 2b）。Needs live bridge.

    跨版本属性漂移（5.0/5.1 bSupportsNanite -> 5.3 NaniteSettings.enabled -> 5.4 再变）
    走多候选 + 回读校验，写不进就 failed+tried，不假成功。半透明材质默认拦截。
    收益是 drawcall/帧时间与日志噪音，不是 MB，故 savings_mb=0，判据看 triangles/blend_modes。
    """
    bus_dir, _pd, err = _resolve(project)
    if err:
        return err
    enabled, e0 = _bool_opt("enabled", enabled, True)
    skip_translucent, e1b = _bool_opt("skip_translucent", skip_translucent, True)
    dry_run, e1 = _bool_opt("dry_run", dry_run, True)
    confirm, e2 = _bool_opt("confirm", confirm, False)
    if e0 or e1b or e1 or e2:
        return e0 or e1b or e1 or e2
    return bus.BusClient(bus_dir, timeout=300).call(
        "opt_mesh_set_nanite",
        {"paths": paths, "folder": folder, "map": map, "enabled": enabled,
         "skip_translucent": skip_translucent, "limit": int(limit),
         "dry_run": dry_run, "confirm": confirm})


def fix_nanite_translucent(paths: Annotated[str | None, Field(description="要处理的网格；给了则忽略 folder/map")] = None, folder: Annotated[str, Field(description="批量范围（默认 /Game）")] = "/Game", map: Annotated[str | None, Field(description="改按关卡 uses 闭包选网格")] = None, limit: Annotated[int, Field(description="本窗口最多处理多少个网格")] = 200, dry_run: Annotated[bool, Field(description="默认 true=只回冲突清单与计划")] = True, confirm: Annotated[bool, Field(description="真实执行需 dry_run=False 且 confirm=True 双钥")] = False, project: Annotated[str | None, Field(description="工程选择器：名称/路径/slug；仅一个活跃工程时可省略")] = None):
    """关掉"已开 Nanite 却挂半透明材质"的网格（写·双钥；v1.5 Feature 2b）。Needs live bridge.

    只动这一类冲突，正常 Nanite 网格不碰；材质 blend_mode 读不到时跳过（不误关）。
    关闭是降级不是损坏：网格按原 LOD 正常渲染，编辑器日志的成万条警告随之消失。
    """
    bus_dir, _pd, err = _resolve(project)
    if err:
        return err
    dry_run, e1 = _bool_opt("dry_run", dry_run, True)
    confirm, e2 = _bool_opt("confirm", confirm, False)
    if e1 or e2:
        return e1 or e2
    return bus.BusClient(bus_dir, timeout=300).call(
        "fix_nanite_translucent",
        {"paths": paths, "folder": folder, "map": map, "limit": int(limit),
         "dry_run": dry_run, "confirm": confirm})


def analyze_instancing(min_count: Annotated[int, Field(description="同网格重复多少个才算合批组（默认 5）")] = 5, cap: Annotated[int, Field(description="groups 返回条数上限")] = 40, include_samples: Annotated[int, Field(description="每组采样多少 Actor 名（便于核对）")] = 10, class_contains: Annotated[str | None, Field(description="按 Actor 类名子串过滤")] = None, tag: Annotated[str | None, Field(description="按 Actor tag 过滤")] = None, export: Annotated[bool, Field(description="true=另把方案落一份 JSON 到 <bus>/plans/（只写方案文件，不碰关卡与 Content）")] = False, project: Annotated[str | None, Field(description="工程选择器：名称/路径/slug；仅一个活跃工程时可省略")] = None):
    """当前关卡的 HISM/ISM 合并方案（只读；v1.5 Feature 3a + D2 路线 B）。Needs live bridge.

    分组键=网格包路径；给可折叠 Actor 数与 drawcall 前/后估算，并把混入蓝图放置器
    （非 StaticMeshActor）的组标成 blockers——合并前必须人工确认。
    默认只回方案不动场景；真要折叠成 HISM 属高风险写操作，需另开 PR 且先提交 VCS。
    """
    bus_dir, _pd, err = _resolve(project)
    if err:
        return err
    env = bus.BusClient(bus_dir, timeout=240).call(
        "analyze_instancing",
        {"min_count": int(min_count), "cap": int(cap),
         "include_samples": int(include_samples), "class_contains": class_contains,
         "tag": tag})
    if _as_bool(export, default=False) and env.get("ok"):
        env["result"]["export"] = _export_instancing_plan(bus_dir, env["result"])
    return env


def _export_instancing_plan(bus_dir, result):
    """路线 B 落盘：只把方案写成 JSON（<bus>/plans/），不触碰关卡/Content/资产。"""
    import json as _json
    import time as _time
    folder = os.path.join(str(bus_dir), "plans")
    stamp = "%s_%d" % (_time.strftime("%Y%m%dT%H%M%SZ", _time.gmtime()),
                       int(_time.time() * 1000) % 1000)
    path = os.path.join(folder, "instancing_%s.json" % stamp)
    tmp = path + ".tmp"
    payload = {"stamp": stamp, "summary": result.get("summary"),
               "groups": result.get("groups"), "plan": result.get("plan"),
               "world_total_actors": result.get("world_total_actors")}
    try:
        os.makedirs(folder, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            _json.dump(payload, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, path)   # 原子写：别让半截计划被读到
    except OSError as exc:
        return {"ok": False, "error": str(exc)[:200], "path": path}
    return {"ok": True, "path": path,
            "note": "只落方案文件（路线 B）；未改关卡 Actor、未写 Content"}


def opt_collapse_hism(mode: Annotated[str, Field(description="plan=只出方案（默认）；collapse=真折叠（写关卡）；restore=按回滚清单重建源 Actor")] = "plan", mesh: Annotated[str | None, Field(description="只处理网格包路径含此子串的组；不填=按 limit 取最大的若干组")] = None, min_count: Annotated[int, Field(description="同网格重复多少个 Actor 才算可折叠组")] = 5, limit: Annotated[int, Field(description="本轮最多折叠多少个组（一组=一个 HISM）")] = 20, max_actors: Annotated[int, Field(description="本窗口最多扫描多少个关卡 Actor")] = 20000, class_contains: Annotated[str | None, Field(description="按 Actor 类名子串过滤")] = None, tag: Annotated[str | None, Field(description="按 Actor tag 过滤")] = None, manifest: Annotated[str | None, Field(description="collapse=回滚清单输出路径（默认 <Project>/Saved/Prism/hism_collapse_<时间戳>.json）；restore=要回滚的清单路径（必填）")] = None, allow_blockers: Annotated[bool, Field(description="false=组内混有非 StaticMeshActor（蓝图放置器）时整组拒收，默认拒收")] = False, allow_material_overrides: Annotated[bool, Field(description="false=源 Actor 带材质覆盖时整组拒收（折叠会丢覆盖），默认拒收")] = False, dry_run: Annotated[bool, Field(description="默认 true=只回方案，不动关卡")] = True, confirm: Annotated[bool, Field(description="真实执行需 dry_run=False 且 confirm=True 双钥")] = False, project: Annotated[str | None, Field(description="工程选择器：名称/路径/slug；仅一个活跃工程时可省略")] = None):
    """把同网格散装 Actor 折叠成 HISM（写·双钥；路线 A，v1.5 Feature 3b）。Needs live bridge.

    mode=plan 只出方案与拒收原因（blockers / 材质覆盖 / over_limit）；mode=collapse 建 HISM、
    把源 Actor 的世界变换写进实例并销毁源 Actor，同时落一份 JSON 回滚清单；mode=restore
    按清单重建源 Actor 并销毁 HISM。
    UE 对关卡 Actor 级改动没有事务回滚（undo 栈不可用）：落盘前先提交 VCS 或另存关卡副本，
    清单只保类/网格/变换/标签/子关卡，碰撞与光照绑定不承诺恢复。
    """
    bus_dir, _pd, err = _resolve(project)
    if err:
        return err
    dry_run, e1 = _bool_opt("dry_run", dry_run, True)
    confirm, e2 = _bool_opt("confirm", confirm, False)
    allow_blockers, e3 = _bool_opt("allow_blockers", allow_blockers, False)
    allow_mo, e4 = _bool_opt("allow_material_overrides", allow_material_overrides, False)
    if e1 or e2 or e3 or e4:
        return e1 or e2 or e3 or e4
    return bus.BusClient(bus_dir, timeout=900).call(
        "opt_collapse_hism",
        {"mode": str(mode), "mesh": mesh, "min_count": int(min_count),
         "limit": int(limit), "max_actors": int(max_actors),
         "class_contains": class_contains, "tag": tag, "manifest": manifest,
         "allow_blockers": allow_blockers, "allow_material_overrides": allow_mo,
         "dry_run": dry_run, "confirm": confirm})


def scan_mesh_lod(folder: Annotated[str, Field(description="Content 下目录前缀（默认 /Game）")] = "/Game", map: Annotated[str | None, Field(description="改按关卡 uses 闭包取网格")] = None, paths: Annotated[str | None, Field(description="只查这些网格")] = None, min_triangles: Annotated[int, Field(description="LOD0 三角数低于此值不算候选（默认 20 万，同 rules.mesh_tri warn）")] = 200000, max_assets: Annotated[int, Field(description="本窗口最多度量多少个网格")] = 200, top: Annotated[int, Field(description="items 返回条数上限")] = 40, offset: Annotated[int, Field(description="分页偏移")] = 0, project: Annotated[str | None, Field(description="工程选择器：名称/路径/slug；仅一个活跃工程时可省略")] = None):
    """LOD 阶梯只读诊断（复盘 #13 · 1a）。Needs live bridge.

    给每个 StaticMesh 的 LOD 数、每级三角数、LOD1/LOD0 降比、Nanite 状态、
    `auto_computed`（阈值是否引擎自动算，探针实证每级 ScreenSize 在 UE Python
    读不到也写不到）与每平台 Min LOD 覆盖，并汇出 `candidates.force_min_lod`
    作为 opt_mesh_set_min_lod 的输入。收益是三角数（单实例单帧），不是 MB，
    不能与纹理池那本账相加；Nanite 已开的网格不进降级候选。
    """
    bus_dir, _pd, err = _resolve(project)
    if err:
        return err
    return bus.BusClient(bus_dir, timeout=240).call(
        "scan_mesh_lod",
        {"folder": folder, "map": map, "paths": paths, "min_triangles": int(min_triangles),
         "max_assets": int(max_assets), "top": int(top), "offset": int(offset)})


def opt_mesh_set_min_lod(paths: Annotated[str | None, Field(description="要改的网格；给了则忽略 folder/map")] = None, folder: Annotated[str, Field(description="批量范围（默认 /Game）")] = "/Game", map: Annotated[str | None, Field(description="改按关卡 uses 闭包选网格")] = None, platform: Annotated[str, Field(description="写哪个平台键的 Min LOD（默认 ALL；实测 ALL/Windows/WindowsNoEditor 均可读写）")] = "ALL", min_lod: Annotated[int | None, Field(description="目标级号；留空=逐网格取最深级(num_lods-1)，-1=清除覆盖回引擎默认")] = None, skip_nanite: Annotated[bool, Field(description="默认 true=Nanite 网格不动（细节由 Nanite 管，强制降级只损画质）")] = True, min_triangles: Annotated[int, Field(description="LOD0 低于此三角数默认跳过（低面数强制降级没收益，0=全改）")] = 200000, limit: Annotated[int, Field(description="本窗口最多处理多少个网格")] = 200, offset: Annotated[int, Field(description="分页偏移")] = 0, manifest: Annotated[str | None, Field(description="执行时原值清单的落盘路径（默认 <Project>/Saved/Prism/minlod_*.json）；dry-run 不落盘")] = None, restore: Annotated[str | None, Field(description="回滚模式：传之前落盘的清单路径，按记录的 before 逐条写回（同样双钥）")] = None, dry_run: Annotated[bool, Field(description="默认 true=只回计划")] = True, confirm: Annotated[bool, Field(description="真实执行需 dry_run=False 且 confirm=True 双钥")] = False, project: Annotated[str | None, Field(description="工程选择器：名称/路径/slug；仅一个活跃工程时可省略")] = None):
    """强制每平台 Min LOD（写·双钥；复盘 #13 · 1b）。Needs live bridge.

    LOD 族唯一被探针实证「可写且回读相符」的动作面：每级 ScreenSize 阈值在 UE Python
    读不到也写不到，所以本工具不碰阈值。写入走 set_minimum_lod_for_platform + 回读校验，
    不符即 failed（附实测值），绝不假成功；执行时落一份原值清单，restore=<清单路径> 可回滚。
    闸门：Nanite 网格默认不动、低面数默认跳过、min_lod >= num_lods 一律拒写。
    收益是三角数（单实例单帧），不是 MB，不与纹理池那本账相加。
    """
    bus_dir, _pd, err = _resolve(project)
    if err:
        return err
    skip_nanite, e0 = _bool_opt("skip_nanite", skip_nanite, True)
    dry_run, e1 = _bool_opt("dry_run", dry_run, True)
    confirm, e2 = _bool_opt("confirm", confirm, False)
    if e0 or e1 or e2:
        return e0 or e1 or e2
    want = None
    if min_lod is not None and str(min_lod).strip() != "":
        try:
            want = int(min_lod)
        except (TypeError, ValueError):
            want = None
    return bus.BusClient(bus_dir, timeout=300).call(
        "opt_mesh_set_min_lod",
        {"paths": paths, "folder": folder, "map": map, "platform": str(platform or "ALL"),
         "min_lod": want, "skip_nanite": skip_nanite, "min_triangles": int(min_triangles),
         "limit": int(limit), "offset": int(offset), "manifest": manifest,
         "restore": restore, "dry_run": dry_run, "confirm": confirm})


_TOOLS = (

    ping, list_level_actors, describe_asset, get_asset_references, get_asset_metrics,
    get_asset_chain, scan_orphan_assets, scan_broken_references,
    read_editor_log, scan_folder_assets,
    cook_package, get_cook_status, attribute_cook_errors,
    get_perf_report, list_perf_rules,
    list_projects, preview_asset_migration, migrate_asset_rename, migrate_asset_move, migrate_asset,
    fix_broken_references,
    scan_texture_streaming, opt_texture_set_virtual, opt_texture_clamp_max_size,
    opt_texture_set_compression, scan_nanite_candidates, opt_mesh_set_nanite,
    fix_nanite_translucent, analyze_instancing, opt_collapse_hism,
    scan_missing_source_files,
    scan_mesh_lod, opt_mesh_set_min_lod,
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