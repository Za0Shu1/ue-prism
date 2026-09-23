"""资产层只读工具：describe_asset / get_asset_references（经 bridge，在编辑器内执行）。

对应 docs/REQUIREMENTS.md §2 S3「引用」与 §6 已知风险。AssetRegistry 方法签名跨 5.0-5.8
有漂移 -> 「多候选签名 + best-effort」，取到即用、取不到返回空并带 note，绝不裸崩。
无引擎（总线降级测试）时结构不变、返回空结果。

5.4 实测（本仓库 scripts/calibrate_assets.py + 探针校准）：
  get_dependencies(package_name:Name, dependency_options:AssetRegistryDependencyOptions) -> Array[Name]
  get_referencers (package_name:Name, reference_options :AssetRegistryDependencyOptions) -> Array[Name]
  —— 单个 Name（非数组！），options 各 include_* 需置 True 才拿到数据；仅「磁盘态」一层引用。
  AssetData.asset_class_path -> Name(旧) 或 TopLevelAssetPath{asset_name}(5.5+)。
"""
from __future__ import annotations

import os

from .. import envelope  # 纯标准库信封（三层通用，非 mcp/网络依赖）
from . import register


def _normalize(asset_path):
    """/Game/A/B[.B][:Sub] -> (package_name=/Game/A/B, object_path=/Game/A/B.B, leaf=B)"""
    s = (asset_path or "").strip().strip('"')
    s = s.split(":", 1)[0]
    base, dot, last = s.rpartition(".")
    if not dot or "/" in last:
        package = s
        leaf = s.rpartition("/")[2]
        object_path = s + "." + leaf if leaf else s
    else:
        package = base
        leaf = last
        object_path = s
    return package, object_path, leaf


def _name_str(x):
    try:
        return x.to_string() if hasattr(x, "to_string") else str(x)
    except Exception:
        return str(x)


def _registry():
    try:
        import unreal
    except Exception:
        return None
    try:
        return unreal.AssetRegistryHelpers.get_asset_registry()
    except Exception:
        try:
            return unreal.AssetRegistry.get_asset_registry()
        except Exception:
            return None


def _class_name(unreal, data):
    """asset_class_path -> 末级类名（Material/Texture2D/World…）；两种返回形态兜底。"""
    try:
        acp = data.asset_class_path
    except Exception:
        acp = None
    # 5.5+/5.4 TopLevelAssetPath 结构：.asset_name
    try:
        an = getattr(acp, "asset_name", None)
        if an is not None and _name_str(an):
            return _name_str(an)
    except Exception:
        pass
    # 旧版：asset_class_path 是 Name "/Script/Engine.Texture2D"
    s = _name_str(acp) if acp is not None else ""
    if s and "asset_name" not in s:
        tail = s.rpartition(".")[2]
        if tail:
            return tail
    try:  # 最后兜底：已加载实例的类名
        obj = data.get_asset()
        if obj is not None:
            return obj.get_class().get_name()
    except Exception:
        pass
    return None


def _disk_size(unreal, package):
    """/Game/... -> <project>/Content/....uasset|.umap 的磁盘字节；取不到返回 None。"""
    try:
        content = unreal.Paths.project_content_dir()  # 末尾含 /
        rel = package[len("/Game/"):] if package.startswith("/Game/") else package.replace("/", os.sep)
        if package.startswith("/Game/"):
            rel = rel.replace("/", os.sep)
        for ext in (".uasset", ".umap"):
            p = os.path.join(content, rel + ext)
            if os.path.isfile(p):
                return p, os.path.getsize(p)
    except Exception:
        pass
    return None, None


def _asset_data(ar, unreal, package, object_path):
    for getter in (
        lambda: ar.get_asset_by_object_path(unreal.Name(object_path)),
        lambda: ar.get_asset_by_object_path(object_path),
    ):
        try:
            data = getter()
        except Exception:
            continue
        if data is not None and (not hasattr(data, "is_valid") or data.is_valid()):
            return data
    try:
        datas = ar.get_assets_by_path(unreal.Name(package), False)
        if datas:
            return datas[0]
    except Exception:
        pass
    return None


@register
def describe_asset(asset_path):
    package, object_path, leaf = _normalize(asset_path)
    result = {"found": False, "query": asset_path, "object_path": object_path, "class": None}
    try:
        import unreal
    except Exception:
        result["note"] = "no_engine"
        return result
    ar = _registry()
    if ar is None:
        # 引擎在场但拿不到注册表：整项能力不可用 → 结构化错误，不伪装成功
        return envelope.make_err(
            envelope.Code.UE_API_MISMATCH,
            "AssetRegistry unavailable: get_asset_registry failed on this engine build",
        )
    data = _asset_data(ar, unreal, package, object_path)
    if data is None:
        result["note"] = "asset_not_found_in_registry"
        return result
    result["found"] = True

    def _try(fn, key):
        try:
            result[key] = fn()
        except Exception:
            pass

    _try(lambda: _name_str(data.package_name), "package_name")
    _try(lambda: _name_str(data.asset_name), "asset_name")
    _try(lambda: _name_str(data.object_path), "resolved_object_path")
    _try(lambda: _class_name(unreal, data), "class")
    try:
        result["is_loaded"] = bool(data.is_asset_loaded())
    except Exception:
        pass
    fp, size = _disk_size(unreal, package)
    if size is not None:
        result["file_path"] = fp
        result["size_bytes"] = size
        result["size_mb"] = round(size / 1048576.0, 4)
    result["api"] = "asset_registry"
    return result


def _dep_options(unreal):
    """构造 AssetRegistryDependencyOptions 并开启各类引用位；不支持则返回 None。"""
    try:
        o = unreal.AssetRegistryDependencyOptions()
    except Exception:
        return None
    for f in (
        "include_hard_package_references", "include_soft_package_references",
        "include_hard_management_references", "include_soft_management_references",
        "include_searchable_names",
    ):
        try:
            o.set_editor_property(f, True)
        except Exception:
            pass
    return o


def _registry_graph(ar, unreal, method, package, recursive):
    """get_dependencies/get_referencers 多签名兜底。返回 (list[str], used_sig|None)。"""
    fn = getattr(ar, method, None)
    if fn is None:
        return [], None
    name = unreal.Name(package)
    opts = _dep_options(unreal)
    out_opts = method + "_options" if opts is not None else None
    candidates = []
    if opts is not None:
        candidates.append((lambda: fn(name, opts), "name+opts"))
        candidates.append((lambda: fn([name], opts), "[name]+opts"))
    candidates.append((lambda: fn(name), "name"))
    candidates.append((lambda: fn([name], bool(recursive)), "[name]+bool"))
    candidates.append((lambda: fn([name]), "[name]"))
    for call, sig in candidates:
        try:
            out = call()
        except Exception:
            continue
        if out is None:
            continue
        try:
            items = [_name_str(x) for x in out]
        except Exception:
            continue
        return items, sig
    return [], None


@register
def get_asset_references(asset_path, direction="both", recursive=False, limit=500):
    direction = (direction or "both").lower()
    if direction not in ("both", "uses", "used_by"):
        direction = "both"
    if isinstance(recursive, str):
        recursive = recursive.strip().lower() in ("1", "true", "yes")
    limit = int(limit)
    package, object_path, _leaf = _normalize(asset_path)
    result = {
        "query": asset_path, "package_name": package, "direction": direction,
        "recursive": bool(recursive), "found": False, "used_by": [], "uses": [],
        "truncated": False, "cap": limit,
    }
    try:
        import unreal
    except Exception:
        result["note"] = "no_engine"
        return result
    ar = _registry()
    if ar is None:
        return envelope.make_err(
            envelope.Code.UE_API_MISMATCH,
            "AssetRegistry unavailable: get_asset_registry failed on this engine build",
        )
    result["found"] = _asset_data(ar, unreal, package, object_path) is not None

    notes = []
    used_sig = {}
    if direction in ("uses", "both"):
        deps, sig = _registry_graph(ar, unreal, "get_dependencies", package, recursive)
        used_sig["uses"] = sig
        result["uses"] = deps[:limit]
        if sig is None:
            notes.append("get_dependencies:api_unavailable")
    if direction in ("used_by", "both"):
        refs, sig = _registry_graph(ar, unreal, "get_referencers", package, recursive)
        used_sig["used_by"] = sig
        result["used_by"] = refs[:limit]
        if sig is None:
            notes.append("get_referencers:api_unavailable")
    result["uses_total"] = len(result["uses"])
    result["used_by_total"] = len(result["used_by"])
    result["truncated"] = (result["uses_total"] >= limit) or (result["used_by_total"] >= limit)
    result["api"] = used_sig
    # 请求的每个方向都无一签名命中：能力完全失效 → 报 UE_API_MISMATCH，不返回空表装ok
    if used_sig and all(s is None for s in used_sig.values()):
        return envelope.make_err(
            envelope.Code.UE_API_MISMATCH,
            "get_dependencies/get_referencers: no candidate signature matched (5.0-5.8 drift?); "
            "tried name+opts/[name]+opts/name/[name]+bool/[name]",
        )
    if recursive and any(s in ("name+opts", "[name]+opts", "name") for s in used_sig.values()):
        notes.append("recursive:best_effort_only(on-disk 1-hop)")
    if notes:
        result["note"] = ";".join(notes)
    return result

@register
def describe_many(paths=None, limit=60):
    """PR-3 批量元数据：单条总线命令完成 N 个 describe_asset（归因富化用，设计稿洞②）。

    items 逐项沿用 describe_asset 返回结构（含自返错误信封，如 UE_API_MISMATCH）。
    """
    paths = list(paths or [])[:int(limit)]
    items = []
    for p in paths:
        try:
            items.append(describe_asset(p))
        except Exception as e:
            items.append({"query": p, "found": False, "note": "runtime_error: %s" % e})
    return {"items": items, "count": len(items), "requested": len(paths)}


@register
def referencers_many(paths=None, cap=10, limit=60):
    """PR-3 批量反向引用计数：used_by_count + 样本（≤cap 条/项），单次总线往返。"""
    paths = list(paths or [])[:int(limit)]
    items = []
    for p in paths:
        package, _obj, _leaf = _normalize(p)
        try:
            r = get_asset_references(p, direction="used_by", recursive=False, limit=int(cap))
            if r.get("ok") is False:  # 自返错误信封：透传 code，归因侧按缺数据降级
                items.append({"query": package, "package_name": package,
                            "error": r["error"]["code"]})
            else:
                res = r["result"] if isinstance(r.get("result"), dict) else r  # 信封或降级裸 result
                items.append({"query": package, "package_name": package,
                            # 降级早退分支无 *_total（引擎在线才有）：就地兜底
                            "used_by_count": res.get("used_by_total", len(res["used_by"])),
                            "used_by_sample": res["used_by"][:int(cap)],
                            "found": res["found"]})
        except Exception as e:
            items.append({"query": package, "package_name": package,
                        "error": "runtime_error", "note": str(e)[:200]})
    return {"items": items, "count": len(items), "requested": len(paths)}



def _bfs_closure(hop, root, max_nodes=2000, max_depth=0):
    """通用逐跳 BFS 闭包（纯逻辑，无引擎可测）。hop(pkg)->(deps, sig)。
    返回 (nodes[level 从1起], total, truncated, depth_reached, api_sig, dep_map)。
    dep_map={已展开包: [其原始依赖]}（含 root、未过滤）：闭包元数据只留首次发现，
    环检测必须靠邻接表把回边留住——见 _find_cycles。"""
    from collections import deque
    seen = set([root])
    queue = deque([(root, 0)])
    nodes = []
    dep_map = {}
    api_sig = None
    truncated = False
    while queue:
        cur, lvl = queue.popleft()
        if max_depth and lvl >= max_depth:
            continue
        deps, sig = hop(cur)
        dep_map[cur] = list(deps)  # 邻接表留痕：回边/交叉边不随去重丢失
        if api_sig is None and sig:
            api_sig = sig
        for d in deps:
            if d in seen:
                continue
            if len(nodes) >= max_nodes:
                truncated = True
                break
            seen.add(d)
            nodes.append({"package": d, "level": lvl + 1})
            queue.append((d, lvl + 1))
        if truncated:
            break
    depth_reached = 0
    for n in nodes:
        if n["level"] > depth_reached:
            depth_reached = n["level"]
    return nodes, len(nodes), truncated, depth_reached, api_sig, dep_map


def _find_cycles(dep_map, in_scope, root, cap=20):
    """环检测（纯逻辑，无引擎可测）：在 (root ∪ in_scope) 诱导子图上跑 Kosaraju 迭代 SCC。
    BFS 去重会把回边静默丢掉——真环(双向耦合、迁移不可拆分)与 DAG 交叉边在闭包里长得一样，
    只有拿邻接表重算 SCC 才分得开。自环(size=1 且自指)也算环。迭代实现：不受递归深度限制(3.7)。
    返回 (cycles, truncated)：cycles=[{members, size, contains_root, self_loop}]，按 size 降序、截到 cap。"""
    nodes = set(in_scope)
    nodes.add(root)
    fwd = {}
    rev = {}
    self_loops = set()
    for a in nodes:
        fwd[a] = []
        rev[a] = []
    for a in nodes:
        for d in dep_map.get(a, ()):
            if d not in nodes:
                continue
            if d == a:
                self_loops.add(a)
                continue
            fwd[a].append(d)
            rev[d].append(a)
    visited = set()
    order = []
    for start in nodes:
        if start in visited:
            continue
        visited.add(start)
        stack = [(start, iter(fwd[start]))]
        while stack:
            node, it = stack[-1]
            pushed = False
            for nxt in it:
                if nxt not in visited:
                    visited.add(nxt)
                    stack.append((nxt, iter(fwd[nxt])))
                    pushed = True
                    break
            if not pushed:
                order.append(node)
                stack.pop()
    comp_seen = set()
    cycles = []
    for start in reversed(order):
        if start in comp_seen:
            continue
        comp_seen.add(start)
        comp = [start]
        stack = [start]
        while stack:
            node = stack.pop()
            for nxt in rev[node]:
                if nxt not in comp_seen:
                    comp_seen.add(nxt)
                    comp.append(nxt)
                    stack.append(nxt)
        if len(comp) > 1 or comp[0] in self_loops:
            cycles.append({"members": sorted(comp), "size": len(comp),
                           "contains_root": root in comp,
                           "self_loop": len(comp) == 1})
    cycles.sort(key=lambda c: (-c["size"], c["members"][0]))
    return cycles[:cap], len(cycles) > cap


def _game_ref_count(ar, unreal, package):
    """单包 /Game 反向引用计数（god-asset 判定用）：复用 _registry_graph 一跳，滤引擎与自身。
    返回 (count, sig)；候选签名全失败返回 (None, None)——上层记未测，绝不把测不到伪装成 0。"""
    refs, sig = _registry_graph(ar, unreal, "get_referencers", package, False)
    if sig is None:
        return None, None
    count = 0
    for r in refs:
        if r.startswith("/Game/") and r != package:
            count += 1
    return count, sig


@register
def get_asset_chain(asset_path, direction="uses", scope="game", with_meta=True, max_nodes=2000, max_depth=0, god_min_refs=30):
    """递归依赖闭包（迁移预览）：direction=uses 取该资产依赖的整条链（迁移要一并带走的东西）；
    used_by 取谁依赖它（挪走会连累谁）。逐跳 BFS + 去重，节点带 level。纯只读，不改任何东西。

    scope=game(默认) 只保留 /Game 工程内容，过滤引擎/Script 噪声（要全量传 scope=all）；
    with_meta=True 给每个节点补 class + 磁盘 size_bytes，并按类别聚合数量/体量总计。
    max_depth=0 不限深度；max_nodes 截断。这正是"迁移一个关卡：要带走哪些资源、多大体量"的答案。
    """
    direction = (direction or "uses").lower()
    if direction not in ("uses", "used_by"):
        direction = "uses"
    scope = (scope or "game").lower()
    if scope not in ("game", "all"):
        scope = "game"
    if isinstance(with_meta, str):
        with_meta = with_meta.strip().lower() in ("1", "true", "yes")
    try:
        max_nodes = int(max_nodes)
    except Exception:
        max_nodes = 2000
    if max_nodes < 1:
        max_nodes = 1
    try:
        max_depth = int(max_depth)
    except Exception:
        max_depth = 0
    try:
        god_min_refs = int(god_min_refs)
    except Exception:
        god_min_refs = 30
    if god_min_refs < 0:
        god_min_refs = 0
    package, object_path, _leaf = _normalize(asset_path)
    try:
        import unreal
    except Exception:
        return {"root": package, "direction": direction, "scope": scope, "found": False,
                "nodes": [], "total": 0, "truncated": False, "cap": max_nodes, "depth_reached": 0,
                "by_class": {}, "total_size_bytes": 0,
                "cycles": [], "cyclic": False, "cycles_truncated": False,
                "god_assets": [], "god_scan": None, "impact_summary": None,
                "note": "no_engine"}
    ar = _registry()
    if ar is None:
        return envelope.make_err(envelope.Code.UE_API_MISMATCH,
                                 "AssetRegistry unavailable: get_asset_registry failed on this engine build")
    method = "get_dependencies" if direction == "uses" else "get_referencers"

    def hop(pkg):
        return _registry_graph(ar, unreal, method, pkg, False)

    found = _asset_data(ar, unreal, package, object_path) is not None
    raw_nodes, _t, truncated, depth_reached, api_sig, dep_map = _bfs_closure(hop, package, max_nodes, max_depth)
    if api_sig is None:
        return envelope.make_err(envelope.Code.UE_API_MISMATCH,
                                 "依赖跳取失败：%s 无候选签名命中（5.0-5.8 漂移？）" % method)

    def _pkg_meta(pkg):
        leaf = pkg.rpartition("/")[2]
        cls, size = None, 0
        try:
            data = _asset_data(ar, unreal, pkg, pkg + "." + leaf) if leaf else None
            if data is not None:
                cls = _class_name(unreal, data)
        except Exception:
            pass
        try:
            _fp, sz = _disk_size(unreal, pkg)
            size = int(sz) if sz else 0
        except Exception:
            pass
        return cls, size

    kept = []
    n_engine = 0
    n_script = 0
    for n in raw_nodes:
        pkg = n["package"]
        if pkg.startswith("/Script"):
            n_script += 1
            if scope == "game":
                continue
        elif not pkg.startswith("/Game/"):
            n_engine += 1
            if scope == "game":
                continue
        node = {"package": pkg, "level": n["level"]}
        if with_meta:
            cls, size = _pkg_meta(pkg)
            node["class"] = cls
            node["size_bytes"] = size
        kept.append(node)

    by_class = {}
    total_size = 0
    for node in kept:
        c = node.get("class") or "?"
        sz = node.get("size_bytes", 0)
        slot = by_class.setdefault(c, {"count": 0, "size_bytes": 0})
        slot["count"] += 1
        slot["size_bytes"] += sz
        total_size += sz
    kept.sort(key=lambda d: (d["level"], d.get("class") or "", d["package"]))

    root_class, root_size = None, 0
    if with_meta:
        try:
            rd = _asset_data(ar, unreal, package, object_path)
            if rd is not None:
                root_class = _class_name(unreal, rd)
            _fp, rs = _disk_size(unreal, package)
            root_size = int(rs) if rs else 0
        except Exception:
            pass

    result = {
        "root": package, "root_class": root_class, "root_size_bytes": root_size,
        "direction": direction, "scope": scope, "found": found,
        "nodes": kept, "total": len(kept),
        "truncated": truncated, "cap": max_nodes, "depth_reached": depth_reached,
        "by_class": by_class, "total_size_bytes": total_size, "total_size_mb": round(total_size / 1048576.0, 4),
        "excluded": {"engine": n_engine, "script": n_script},
        "api": {"hop": api_sig},
    }
    # —— 加固① 环检测：(root ∪ kept) 诱导子图 SCC；BFS 去重丢掉的回边在这里显式点名 ——
    kept_pkgs = [n["package"] for n in kept]
    cycles, cycles_truncated = _find_cycles(dep_map, kept_pkgs, package)
    result["cycles"] = cycles
    result["cyclic"] = bool(cycles)
    result["cycles_truncated"] = cycles_truncated

    # —— 加固② god-asset：uses 闭包里的共享枢纽（/Game 引用者 >= god_min_refs）——
    #     枢纽是全库共享资产，"留在原地被引用"才是迁移正解；used_by 方向本身就是引用者名单，不重复判。
    if direction == "uses" and god_min_refs > 0 and kept:
        scan_cap = 400  # 编辑器内逐包 get_referencers：控单命令耗时，大闭包按体量优先扫
        to_scan = sorted(kept, key=lambda d: -int(d.get("size_bytes") or 0))[:scan_cap]
        hubs = []
        hub_sig = None
        for node in to_scan:
            cnt, sig = _game_ref_count(ar, unreal, node["package"])
            if cnt is None:
                continue
            if hub_sig is None:
                hub_sig = sig
            node["used_by_game"] = cnt
            node["god_asset"] = cnt >= god_min_refs
            if node["god_asset"]:
                hubs.append(node)
        hubs.sort(key=lambda d: -d["used_by_game"])
        result["god_assets"] = [
            {"package": h["package"], "class": h.get("class"), "level": h["level"],
             "used_by_game": h["used_by_game"], "size_bytes": h.get("size_bytes", 0)}
            for h in hubs[:20]]
        result["god_scan"] = {"scanned": len(to_scan), "of": len(kept),
                              "min_refs": god_min_refs, "hub_total": len(hubs), "sig": hub_sig}
        if len(to_scan) < len(kept):
            result["god_scan"]["note"] = "枢纽扫描仅覆盖体量最大的 %d 个节点，其余未测" % len(to_scan)
    elif direction == "uses":
        result["god_assets"] = []
        result["god_scan"] = {"scanned": 0, "of": len(kept), "min_refs": 0,
                              "hub_total": 0, "note": "disabled (god_min_refs=0)"}
    else:
        result["god_assets"] = []
        result["god_scan"] = None

    # —— 加固③ 影响半径摘要：used_by 方向给"波及 N 关卡 + 按类计数 + 总体量"的一行决策结论 ——
    if direction == "used_by":
        if with_meta:
            worlds = [n for n in kept if n.get("class") == "World"]
            result["impact_summary"] = {
                "referencers_total": len(kept),
                "worlds_total": len(worlds),
                "worlds": [w["package"] for w in worlds[:20]],
                "worlds_truncated": len(worlds) > 20,
                "by_class": by_class,
                "total_size_mb": result["total_size_mb"],
            }
        else:
            result["impact_summary"] = {"referencers_total": len(kept),
                                        "note": "with_meta=False：无类名，无法按关卡/类别拆分影响面"}
    else:
        result["impact_summary"] = None

    notes = []
    if scope == "game" and (n_engine or n_script):
        notes.append("已过滤引擎/Script 依赖（迁移仅需 /Game）；scope=all 可含全量")
    if cycles:
        root_in = any(c["contains_root"] for c in cycles)
        notes.append("检出环 %d 处%s：闭包内双向耦合，不可拆分子集迁移"
                     % (len(cycles), "（根资产卷入）" if root_in else ""))
    elif truncated:
        notes.append("闭包被截断：环检测可能漏报，cyclic=False 不完全可信")
    if cycles_truncated:
        notes.append("环列表已截断（上限 20 组）")
    if notes:
        result["note"] = "; ".join(notes)
    return result

# ---------------- scan_orphan_assets：孤儿资产扫描（只读·深度分析 P0） ----------------
# 复用顶部 helper：_registry/_class_name/_disk_size/_registry_graph/_name_str + envelope。
# 假阳性排除/可信度分级是核心难点：见 _orphan_exclusion / _orphan_confidence（纯逻辑，无引擎可测）。

_WP_MARKERS = ("__ExternalActors__", "__ExternalObjects__")
# 工程内常见『按名字/路径动态加载』或『本就是入口』的目录/类型线索，命中则孤儿可信度降为 suspect。
_SCRATCH_HINTS = ("/Editor/", "/Debug/", "/Old/", "/Backup/", "/Test/", "/Temp/", "/Draft/")


def _orphan_exclusion(class_name, package):
    """该 /Game 包是否属于『不该被当成孤儿』的固有类型？返回 reason 字符串或 None。纯逻辑。"""
    if not package.startswith("/Game/"):
        return "non_game"
    if class_name == "World":
        return "level_root"          # 关卡是运行时入口，不靠被引用
    for mk in _WP_MARKERS:
        if mk in package:
            return "external_wp"      # World Partition 外部 actor/obj，由 map 隐式加载
    leaf = package.rpartition("/")[2]
    if class_name == "Redirector" or "_Redirector" in leaf or leaf.startswith("REINST_"):
        return "redirector"           # 重定向桩，本就是待清理占位
    return None


def _orphan_confidence(class_name, package, has_primary_id, mtime_age_days, recent_days):
    """给『无人引用』的资产打可信度。返回 (confidence, reasons[list])。
    suspect = 可能被静态引用图捕获不到的方式加载（按名/软路径/动态字符串）或刚改动，删除风险高。纯逻辑。"""
    reasons = []
    if has_primary_id:
        reasons.append("primary_asset_id:可能按名加载")
    if class_name in ("Blueprint", "BlueprintGeneratedClass", "AnimBlueprint"):
        reasons.append("blueprint:可能按路径实例化")
    if any(h in package for h in _SCRATCH_HINTS):
        reasons.append("editor_or_scratch_path")
    if recent_days is not None and mtime_age_days is not None and mtime_age_days < recent_days:
        reasons.append("recently_modified")
    segs = [s for s in package.split("/") if s and s != "Game"]
    if any(s.startswith("_") for s in segs):
        reasons.append("underscore_prefixed_dir")
    return ("suspect" if reasons else "high"), reasons


def _assets_under(ar, unreal, folder):
    """枚举 folder（含递归）下所有 AssetData。多签名兜底；全失败返回 None（调用方报 API_MISMATCH）。"""
    name = unreal.Name(folder)
    for call in (
        lambda: ar.get_assets_by_path(name, True),
        lambda: ar.get_assets_by_path(name),
    ):
        try:
            out = call()
        except Exception:
            continue
        if out is None:
            continue
        try:
            return list(out)
        except Exception:
            continue
    return None


def _has_primary_asset_id(unreal, data):
    """AssetData 是否有有效 PrimaryAssetId（可能按名加载）。best-effort，取不到判 False。"""
    try:
        pid = data.get_primary_asset_id()
        s = _name_str(pid)
        return bool(s and s.lower() not in ("none", ""))
    except Exception:
        pass
    try:
        pai = getattr(data, "primary_asset_id", None)
        if pai is None:
            return False
        name = getattr(pai, "asset_name", None)
        if name is not None:
            s = _name_str(name)
            return bool(s and s.lower() not in ("none", ""))
    except Exception:
        pass
    return False


def _cascade_killset(ar, unreal, seed, max_nodes=2000):
    """级联可回收上限: 从『种子孤儿』做不动点扩散, 找出**仅被这批孤儿引用**的依赖包。
    删掉种子孤儿后, 这些依赖会变成无人引用的二级孤儿 -> 一并可回收。
    判定: 候选 X 的全部引用者(含引擎/外部)都已在 killset 内, 才可加入; 只要有一个外部引用者就永久保留。
    返回 (added_packages:list, truncated:bool)。只在『孤儿子图』上跑 uses+referencers, 规模可控。"""
    killset = set(seed)
    frontier = list(seed)
    added = []
    seen = set(seed)
    truncated = False
    passes = 0
    while frontier and passes < 50 and not truncated:
        passes += 1
        candidates = set()
        for pkg in frontier:
            deps, _sig = _registry_graph(ar, unreal, "get_dependencies", pkg, False)
            for d in deps:
                if d.startswith("/Game/") and d not in seen:
                    candidates.add(d)
        seen |= candidates
        new_frontier = []
        for x in sorted(candidates):
            if len(killset) >= max_nodes:
                truncated = True
                break
            refs, _sig = _registry_graph(ar, unreal, "get_referencers", x, False)
            if refs and all(r in killset for r in refs):
                killset.add(x)
                added.append(x)
                new_frontier.append(x)
        frontier = new_frontier
    return added, truncated


@register
def scan_orphan_assets(folder="/Game", limit=2000, offset=0, max_orphans=200, recent_days=14, cascade=True):
    """扫描 folder 下的『孤儿资产』：在 AssetRegistry 引用图里没有任何引用者(referencers=0)的 /Game 包。
    只读。输出可回收体量(total_reclaimable_mb)+按目录聚合+逐条 size/可信度，直接给瘦身清单。

    cascade=True(默认): 追加**级联可回收上限**——把『仅被这批孤儿引用』的依赖(删种子后必成二级孤儿)一并计入,
      给出 cascading_total_reclaimable_mb(真实可回收上限, >= 种子值)。种子字段(total_reclaimable_mb/orphans)口径不变。
    工程越大越慢：limit 控制本窗口最多检查多少资产(逐个 get_referencers)，offset 翻页；orphans 再按 size 降序截到 max_orphans。
    静态引用图无法捕获运行时拼字符串加载(FName/LoadObject by path)——此类资产可能被判 orphan，故有 confidence=suspect 分级，删除前务必二次校验+走版本管理。
    """
    import time as _time
    if isinstance(cascade, str):
        cascade = cascade.strip().lower() in ("1", "true", "yes")
    cascade = bool(cascade)
    folder = (folder or "/Game").strip().strip('"')
    if not folder.startswith("/Game"):
        folder = "/Game/" + folder.lstrip("/")
    try:
        limit = max(1, int(limit))
    except Exception:
        limit = 2000
    try:
        offset = max(0, int(offset))
    except Exception:
        offset = 0
    try:
        max_orphans = max(1, int(max_orphans))
    except Exception:
        max_orphans = 200
    try:
        recent_days = int(recent_days)
    except Exception:
        recent_days = 14

    result = {
        "folder": folder, "found_registry": False, "cascade_enabled": cascade,
        "total_scanned": 0, "total_available": 0,
        "truncated": False, "cap": limit, "offset": offset,
        "orphans": [], "orphan_count": 0,
        "total_reclaimable_bytes": 0, "total_reclaimable_mb": 0.0,
        "by_dir": {}, "by_dir_truncated": False,
        "excluded": {}, "confidence_counts": {"high": 0, "suspect": 0},
        "orphans_truncated": False,
        "cascading_orphan_count": 0,
        "cascade_added_count": 0, "cascade_reclaimable_bytes": 0, "cascade_reclaimable_mb": 0.0,
        "cascading_total_reclaimable_bytes": 0, "cascading_total_reclaimable_mb": 0.0,
        "cascade_nodes": [], "cascade_nodes_truncated": False, "cascade_truncated": False,
        "note": ("静态引用图(AssetRegistry，含 soft)；无法捕获运行时拼字符串加载。"
                 "confidence=suspect 者删除前务必二次校验并先纳入版本管理。"),
    }
    try:
        import unreal
    except Exception:
        result["note"] = "no_engine: 需编辑器在线(桥)才能枚举资产与引用图。"
        return result
    ar = _registry()
    if ar is None:
        return envelope.make_err(envelope.Code.UE_API_MISMATCH,
                                 "AssetRegistry unavailable: get_asset_registry failed on this engine build")
    result["found_registry"] = True

    datas = _assets_under(ar, unreal, folder)
    if datas is None:
        return envelope.make_err(envelope.Code.UE_API_MISMATCH,
                                 "get_assets_by_path: no candidate signature matched (5.0-5.8 drift?)")

    entries = []
    for d in datas:
        try:
            pkg = _name_str(d.package_name)
        except Exception:
            continue
        if pkg:
            entries.append((pkg, d))
    entries.sort(key=lambda t: t[0])
    total_all = len(entries)
    result["total_available"] = total_all
    window = entries[offset:offset + limit]
    result["total_scanned"] = len(window)
    result["truncated"] = (offset + limit) < total_all

    now = _time.time()
    orphans_all = []
    excluded_counts = {}
    conf_counts = {"high": 0, "suspect": 0}
    by_dir = {}

    for pkg, d in window:
        cls = _class_name(unreal, d)
        ex = _orphan_exclusion(cls, pkg)
        if ex:
            excluded_counts[ex] = excluded_counts.get(ex, 0) + 1
            continue
        refs, sig = _registry_graph(ar, unreal, "get_referencers", pkg, False)
        if sig is None:
            return envelope.make_err(envelope.Code.UE_API_MISMATCH,
                                     "get_referencers: no candidate signature matched (5.0-5.8 drift?)")
        if len(refs) > 0:
            continue  # 被任何资产引用 -> 非孤儿
        has_pid = _has_primary_asset_id(unreal, d)
        fp, size = _disk_size(unreal, pkg)
        size = int(size) if size else 0
        age = None
        if fp:
            try:
                age = (now - os.path.getmtime(fp)) / 86400.0
            except Exception:
                age = None
        conf, reasons = _orphan_confidence(cls, pkg, has_pid, age, recent_days)
        conf_counts[conf] = conf_counts.get(conf, 0) + 1
        orphans_all.append({
            "package": pkg, "class": cls, "size_bytes": size,
            "size_mb": round(size / 1048576.0, 3), "confidence": conf, "reasons": reasons,
        })
        parent = pkg.rpartition("/")[0] or "/"
        slot = by_dir.setdefault(parent, {"count": 0, "size_bytes": 0})
        slot["count"] += 1
        slot["size_bytes"] += size

    total_reclaim = 0
    for o in orphans_all:
        total_reclaim += o["size_bytes"]
    result["orphan_count"] = len(orphans_all)
    result["total_reclaimable_bytes"] = total_reclaim
    result["total_reclaimable_mb"] = round(total_reclaim / 1048576.0, 3)
    result["excluded"] = excluded_counts
    result["confidence_counts"] = conf_counts

    dirs_sorted = sorted(by_dir.items(), key=lambda kv: kv[1]["size_bytes"], reverse=True)
    result["by_dir"] = {k: {"count": v["count"], "size_bytes": v["size_bytes"],
                            "size_mb": round(v["size_bytes"] / 1048576.0, 3)}
                        for k, v in dirs_sorted[:30]}
    result["by_dir_truncated"] = len(dirs_sorted) > 30

    orphans_all.sort(key=lambda o: o["size_bytes"], reverse=True)
    result["orphans"] = orphans_all[:max_orphans]
    result["orphans_truncated"] = len(orphans_all) > max_orphans

    # 级联可回收上限: 从种子孤儿扩散
    seed_pkgs = [o["package"] for o in orphans_all]
    casc_bytes = 0
    cascade_nodes = []
    if cascade and seed_pkgs:
        added, casc_trunc = _cascade_killset(ar, unreal, seed_pkgs, max_nodes=5000)
        result["cascade_truncated"] = casc_trunc
        for pkg in added:
            leaf = pkg.rpartition("/")[2]
            cls = None
            try:
                dd = _asset_data(ar, unreal, pkg, pkg + "." + leaf) if leaf else None
                if dd is not None:
                    cls = _class_name(unreal, dd)
            except Exception:
                cls = None
            if _orphan_exclusion(cls, pkg):  # 关卡/外部 actor/重定向桩不参与级联删除
                continue
            _fp, sz = _disk_size(unreal, pkg)
            sz = int(sz) if sz else 0
            casc_bytes += sz
            cascade_nodes.append({"package": pkg, "class": cls,
                                  "size_bytes": sz, "size_mb": round(sz / 1048576.0, 3)})
        cascade_nodes.sort(key=lambda o: o["size_bytes"], reverse=True)
    result["cascade_added_count"] = len(cascade_nodes)
    result["cascade_reclaimable_bytes"] = casc_bytes
    result["cascade_reclaimable_mb"] = round(casc_bytes / 1048576.0, 3)
    result["cascading_orphan_count"] = len(orphans_all) + len(cascade_nodes)
    casc_total = total_reclaim + casc_bytes
    result["cascading_total_reclaimable_bytes"] = casc_total
    result["cascading_total_reclaimable_mb"] = round(casc_total / 1048576.0, 3)
    result["cascade_nodes"] = cascade_nodes[:max_orphans]
    result["cascade_nodes_truncated"] = len(cascade_nodes) > max_orphans

    if result["truncated"]:
        result["scan_note"] = "本次仅扫描 offset=%d 起 %d 个资产(共 %d)；reclaimable 为该窗口内值，翻页可累计。" % (offset, result["total_scanned"], total_all)
    return result