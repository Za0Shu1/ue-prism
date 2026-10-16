"""资产层只读工具：describe_asset / get_asset_references（经 bridge，在编辑器内执行）。

对应 REQUIREMENTS §2 S3「引用」与 §6 已知风险。AssetRegistry 方法签名跨 5.0-5.8
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


_ALL_INCLUDE = ("hard", "soft", "mgmt", "search")


def _dep_options(unreal, include=_ALL_INCLUDE):
    """构造 AssetRegistryDependencyOptions，按 include 集合开启对应引用位。

    include 为 ("hard","soft","mgmt","search") 的子集；默认全开（与旧行为一致）。
    只开 hard 得到硬引用集合，只开 soft 得到软引用集合，供分类。不支持则返回 None。"""
    try:
        o = unreal.AssetRegistryDependencyOptions()
    except Exception:
        return None
    flags = {
        "include_hard_package_references": "hard" in include,
        "include_soft_package_references": "soft" in include,
        "include_hard_management_references": "mgmt" in include,
        "include_soft_management_references": "mgmt" in include,
        "include_searchable_names": "search" in include,
    }
    for f, v in flags.items():
        try:
            o.set_editor_property(f, v)
        except Exception:
            pass
    return o


def _ref_variant(ar, unreal, method, package, include):
    """用指定引用位组合做一次 get_referencers/get_dependencies；返回 (set[str]|None, sig)。

    仅尝试带 options 的签名（分类必须靠 options 过滤）；拿不到返回 (None, reason)。"""
    fn = getattr(ar, method, None)
    if fn is None:
        return None, method + "_absent"
    opts = _dep_options(unreal, include)
    if opts is None:
        return None, "options_unavailable"
    name = unreal.Name(package)
    for call, sig in ((lambda: fn(name, opts), "name+opts"),
                      (lambda: fn([name], opts), "[name]+opts")):
        try:
            out = call()
        except Exception:
            continue
        if out is None:
            continue
        try:
            return set(_name_str(x) for x in out), sig
        except Exception:
            continue
    return None, "sig_unmatched"


def _merge_detail(hard, soft, limit):
    """hard/soft 两个包名集合 -> [{package, hard, soft}]（并集、排序、截到 limit）。

    None 视作空集；任一 variant 不可用返回的也是空集，detail 相应为空并在 api 里记 reason。"""
    hard = hard or set()
    soft = soft or set()
    union = sorted(hard | soft)
    return [{"package": p, "hard": p in hard, "soft": p in soft}
            for p in union[:limit]]


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
def get_asset_references(asset_path, direction="both", recursive=False, limit=500, classify_soft=False):
    direction = (direction or "both").lower()
    if direction not in ("both", "uses", "used_by"):
        direction = "both"
    if isinstance(recursive, str):
        recursive = recursive.strip().lower() in ("1", "true", "yes")
    if isinstance(classify_soft, str):
        classify_soft = classify_soft.strip().lower() in ("1", "true", "yes")
    limit = int(limit)
    package, object_path, _leaf = _normalize(asset_path)
    result = {
        "query": asset_path, "package_name": package, "direction": direction,
        "recursive": bool(recursive), "found": False, "used_by": [], "uses": [],
        "truncated": False, "cap": limit,
        "classify_soft": bool(classify_soft),
        "used_by_detail": [], "uses_detail": [],
        "used_by_soft_count": 0, "uses_soft_count": 0,
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
    # 硬/软分类（可选）：对每个方向各调 hard-only 与 soft-only，按包并集标注来源。
    # 注意：searchable/mgmt 引用不计入 detail（只覆盖 package 级硬/软）。
    if classify_soft:
        if direction in ("used_by", "both"):
            hard, hs = _ref_variant(ar, unreal, "get_referencers", package, ("hard",))
            soft, ss = _ref_variant(ar, unreal, "get_referencers", package, ("soft",))
            result["used_by_detail"] = _merge_detail(hard, soft, limit)
            result["used_by_soft_count"] = len(soft or set())
            used_sig["used_by_classify"] = [hs, ss]
        if direction in ("uses", "both"):
            hard, hd = _ref_variant(ar, unreal, "get_dependencies", package, ("hard",))
            soft, sd = _ref_variant(ar, unreal, "get_dependencies", package, ("soft",))
            result["uses_detail"] = _merge_detail(hard, soft, limit)
            result["uses_soft_count"] = len(soft or set())
            used_sig["uses_classify"] = [hd, sd]
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


def _export_object_name(unreal, data):
    """AssetData 指向的**导出对象名**（不含包路径）。多签名兜底，取不到返回 ""。"""
    for getter in (lambda: data.get_object_path(), lambda: getattr(data, "object_path", None)):
        try:
            op = getter()
        except Exception:
            continue
        if op is None:
            continue
        s = ""
        try:
            inner = getattr(op, "asset_path", None)
            if inner is not None:
                s = _name_str(inner)
        except Exception:
            s = ""
        if not s or s == "None":
            s = _name_str(op)
        if s and s != "None":
            return s.rpartition(".")[2]
    try:
        name = getattr(data, "asset_name", None)
        return _name_str(name) if name is not None else ""
    except Exception:
        return ""


def _export_object_path(unreal, package, data):
    """导出对应的对象路径。多导出包（HLOD/子资产）不能用 package.leaf 代表全部导出。"""
    leaf = package.rpartition("/")[2]
    return package + "." + (_export_object_name(unreal, data) or leaf)


def _export_score(unreal, data, leaf, prefer_terms):
    """同包多导出的代表度：类匹配 prefer_terms > 导出名==包名(主资产) > primary id > 有类名。"""
    cls = _class_name(unreal, data) or ""
    score = 0
    low = cls.lower()
    if prefer_terms and any(t in low for t in prefer_terms):
        score += 8
    if leaf and _export_object_name(unreal, data) == leaf:
        score += 4
    try:
        if _has_primary_asset_id(unreal, data):
            score += 2
    except Exception:
        pass
    if cls:
        score += 1
    return score


def _dedupe_by_package(datas, unreal, prefer_terms=None):
    """按包去重 -> [(package, AssetData)]，一包只留一条代表导出。

    `get_assets_by_path` 对**多导出包**（HLOD 烘焙图集、子资产、Blueprint 生成类）按
    *导出*返回多条 AssetData。不去重的实测后果（5.4 真实工程）：一个 HLOD 包以子导出
    身份重复 48 次，把 limit=400 的纹理窗口吃掉 124 格、同包重复度量，还把子导出的类名
    当成本包的类。给了 prefer_terms 就留"类匹配且最像主资产"的那条，配合
    `_export_object_path` 让后续读写落在真正的导出对象上。
    """
    order = []
    best = {}
    for data in datas or []:
        try:
            pkg = _name_str(data.package_name)
        except Exception:
            continue
        if not pkg:
            continue
        leaf = pkg.rpartition("/")[2]
        score = _export_score(unreal, data, leaf, prefer_terms)
        cur = best.get(pkg)
        if cur is None:
            best[pkg] = (score, data)
            order.append(pkg)
        elif score > cur[0]:
            best[pkg] = (score, data)
    return [(pkg, best[pkg][1]) for pkg in order]


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
        "ref_query_failed": [], "ref_query_failed_count": 0,
        "ref_query_failed_truncated": False,
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

    # 一包一条（多导出包按导出返回多条，见 _dedupe_by_package）。
    entries = _dedupe_by_package(datas, unreal)
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
    attempted = 0
    ref_failed = []

    for pkg, d in window:
        cls = _class_name(unreal, d)
        ex = _orphan_exclusion(cls, pkg)
        if ex:
            excluded_counts[ex] = excluded_counts.get(ex, 0) + 1
            continue
        attempted += 1
        refs, sig = _registry_graph(ar, unreal, "get_referencers", pkg, False)
        if sig is None:
            # 单个包查不动（内存态复制靶、注册表陈旧项）不得把整场扫描判成引擎 API 漂移：
            # 记一条 unresolved 继续扫，判"引用者未知"=不当孤儿（宁漏不误删）。
            # 5.4 真实工程实证：夹具的 in-memory 复制靶让 folder=/Game/PrismCalib/_pwtmp
            # 首包就抛，旧代码直接 return UE_API_MISMATCH，把整份孤儿清单带走。
            ref_failed.append(pkg)
            continue
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

    result["ref_query_failed_count"] = len(ref_failed)
    result["ref_query_failed"] = ref_failed[:20]
    result["ref_query_failed_truncated"] = len(ref_failed) > 20
    if ref_failed and attempted and len(ref_failed) >= attempted:
        # 整窗都查不动才是真签名漂移，照旧诚实报错（绝不静默出"看似干净"的清单）
        return envelope.make_err(
            envelope.Code.UE_API_MISMATCH,
            "get_referencers: no candidate signature matched (5.0-5.8 drift?) — "
            "本窗 %d 个包全部查询失败" % attempted)
    if ref_failed:
        result["note"] = result.get("note", "") + (
            " 本窗 %d/%d 个包 get_referencers 查询失败(引用者未知，已按非孤儿保守排除，"
            "清单见 ref_query_failed)。" % (len(ref_failed), attempted))

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



def _dep_resolution_status(ar, unreal, dep_pkg):
    """/Game(或 /Engine) 依赖包在注册表里的解析态：ok | missing | redirector。

    _asset_data 解析不到 = missing(目标硬删/改名遗留/坏路径，cook/加载会报 Could not find package)；
    解析到但类名含 Redirector = 可修复的重定向桩；否则 ok。"""
    leaf = dep_pkg.rpartition("/")[2]
    obj = (dep_pkg + "." + leaf) if leaf else dep_pkg
    data = _asset_data(ar, unreal, dep_pkg, obj)
    if data is None:
        return "missing", None
    try:
        cls = _class_name(unreal, data)
    except Exception:
        cls = None
    if cls and "Redirector" in str(cls):
        return "redirector", cls
    return "ok", cls


@register
def scan_broken_references(asset_paths=None, folder="/Game", limit=200, offset=0,
                           max_items=200, include_engine=False):
    """扫描『悬空/坏引用』(只读)：资产的硬依赖(序列化 import 表)指向的包无法解析。

    kind=missing  = 目标 /Game 包已不在 AssetRegistry(被硬删、改名未留桩、或写坏的绝对路径)
                    -> cook/加载会报 Could not find package；通常需从 VCS 还原或重新指向。
    kind=redirector = 目标只剩重定向桩(ObjectRedirector) -> 可被 fix_broken_references 安全修复。

    范围二选一：给 asset_paths 只查这些资产(逐个 get_dependencies)；否则枚举 folder(/Game 前缀可省)。
    默认只看 /Game 依赖(include_engine=True 才把 /Engine 依赖纳入；/Script 原生类引用恒排除)。
    静态口径：不含运行时拼字符串/软对象路径(AssetRegistry 无法反查不存在的软目标)，那些留 L3 真机日志核对。

    limit=本窗口扫描多少资产；offset 翻页；max_items 截 broken/by_dep 明细输出。列表带 total/truncated/cap。
    无引擎(总线降级)结构键常驻、返回空。
    """
    if isinstance(include_engine, str):
        include_engine = include_engine.strip().lower() in ("1", "true", "yes")
    include_engine = bool(include_engine)
    try:
        limit = max(1, int(limit))
    except Exception:
        limit = 200
    try:
        offset = max(0, int(offset))
    except Exception:
        offset = 0
    try:
        max_items = max(1, int(max_items))
    except Exception:
        max_items = 200

    result = {
        "found_registry": False, "scope": None,
        "total_scanned": 0, "total_available": 0,
        "offset": offset, "cap": limit, "truncated": False,
        "assets_with_broken": 0,
        "broken_count": 0, "missing_count": 0, "redirector_count": 0,
        "fixable_redirector_deps": [], "broken": [], "broken_truncated": False,
        "by_dep": [], "api": {},
        "note": ("硬依赖=序列化 import 表；missing=目标包已不在注册表(硬删/改名未留桩/坏路径)，"
                 "cook/加载报 Could not find package。redirector=只剩重定向桩，可用 fix_broken_references 修复。"
                 "软引用/运行时拼路径不在静态口径(见正文)。"),
    }
    ap_list = [str(x).strip() for x in (asset_paths or []) if str(x).strip()]
    folder = (folder or "/Game").strip().strip('"')
    if not folder.startswith("/Game"):
        folder = "/Game/" + folder.lstrip("/")
    try:
        import unreal
    except Exception:
        result["note"] = "no_engine: 需编辑器在线(桥)才能枚举资产与依赖图。"
        return result
    ar = _registry()
    if ar is None:
        return envelope.make_err(envelope.Code.UE_API_MISMATCH,
                                 "AssetRegistry unavailable: get_asset_registry failed on this engine build")
    result["found_registry"] = True

    if ap_list:
        pkgs = []
        for ap in ap_list:
            p = _normalize(ap)[0]
            if p and p not in pkgs:
                pkgs.append(p)
        result["scope"] = "asset_paths"
        total_all = len(pkgs)
    else:
        datas = _assets_under(ar, unreal, folder)
        if datas is None:
            return envelope.make_err(envelope.Code.UE_API_MISMATCH,
                                     "get_assets_by_path: no candidate signature matched (5.0-5.8 drift?)")
        pkgs = []
        for d in datas:
            try:
                pkg = _name_str(d.package_name)
            except Exception:
                continue
            if pkg and pkg not in pkgs:
                pkgs.append(pkg)
        pkgs.sort()
        result["scope"] = "folder:" + folder
        total_all = len(pkgs)
    result["total_available"] = total_all
    window = pkgs[offset: offset + limit]
    result["total_scanned"] = len(window)
    result["truncated"] = (offset + limit) < total_all

    resolution_cache = {}
    dep_referencers = {}
    broken_rows = []
    sigs = set()
    for pkg in window:
        deps, sig = _registry_graph(ar, unreal, "get_dependencies", pkg, False)
        if sig:
            sigs.add(sig)
        if not deps:
            continue
        has_broken = False
        seen = set()
        for dep in deps:
            dep = dep if isinstance(dep, str) else _name_str(dep)
            if not dep or dep == pkg or dep in seen:
                continue
            seen.add(dep)
            if dep.startswith("/Game"):
                pass
            elif include_engine and dep.startswith("/Engine"):
                pass
            else:
                continue
            if dep in resolution_cache:
                st, cls = resolution_cache[dep]
            else:
                st, cls = _dep_resolution_status(ar, unreal, dep)
                resolution_cache[dep] = (st, cls)
            if st == "ok":
                continue
            broken_rows.append({"asset": pkg, "dep": dep, "kind": st, "dep_class": cls})
            dep_referencers.setdefault(dep, set()).add(pkg)
            has_broken = True
        if has_broken:
            result["assets_with_broken"] += 1

    by_dep = []
    for dep, refs in dep_referencers.items():
        st, cls = resolution_cache.get(dep, ("?", None))
        by_dep.append({"dep": dep, "kind": st, "class": cls,
                       "referenced_by_count": len(refs), "referenced_by": sorted(refs)[:10]})
    by_dep.sort(key=lambda r: (0 if r["kind"] == "redirector" else 1, -r["referenced_by_count"], r["dep"]))
    result["by_dep"] = by_dep[:max_items]
    result["broken_count"] = len(broken_rows)
    result["missing_count"] = sum(1 for r in broken_rows if r["kind"] == "missing")
    result["redirector_count"] = sum(1 for r in broken_rows if r["kind"] == "redirector")
    result["broken"] = sorted(broken_rows, key=lambda r: (r["asset"], r["dep"]))[:max_items]
    result["broken_truncated"] = len(broken_rows) > max_items
    result["fixable_redirector_deps"] = sorted({r["dep"] for r in broken_rows if r["kind"] == "redirector"})
    result["api"] = {"get_dependencies": (sorted(sigs) if sigs else None)}
    return result


# ---------------- 源文件在场性诊断（只读 · 零金额 · #11 C） ----------------

_IMPORT_RECORD_METHODS = ("extract_filenames", "script_extract_filenames",
                          "get_first_filename", "script_get_first_filename")
_IMPORT_RECORD_PROPS = ("filenames", "file_names", "source_files")


def _clip(value, n=120):
    """异常/长文本进返回面前先压平，避免把整段栈或超长路径灌进信封。"""
    s = str(value)
    if len(s) > n:
        s = s[:n] + "..."
    return s.replace("\r", " ").replace("\n", " ")


def _src_scan_skip(class_name, package):
    """在场性扫描的排除项：这些类没有「外部原文件」概念，且载入编辑器内存有副作用。

    返回跳过原因或 None。关卡(World)与 World Partition 的外部 Actor/外部对象由关卡流水线
    生成，源=关卡本身；重定向桩是待清理项，不是源丢失。
    """
    if class_name == "World":
        return "level"
    if "__ExternalActors__" in package or "__ExternalObjects__" in package:
        return "external_wp"
    if class_name in ("Redirector", "ObjectRedirector"):
        return "redirector"
    return None


def _project_dir_nc(unreal):
    """工程根的归一化绝对路径（判记录路径是否落在本工程内）；取不到返回 None。"""
    try:
        root = unreal.Paths.project_dir()
    except Exception:
        return None
    try:
        return os.path.normcase(os.path.abspath(root))
    except Exception:
        return None


def _is_under(path, root_nc):
    """记录路径是否落在本工程内（判『美术把源图留在工程里』还是『散在桌面/外置盘』）。

    两侧都过 normcase+abspath，否则 C:/proj 与 C:\\proj 混排会整体误判成工程外。
    取不到工程根返回 False（不猜）。
    """
    if not root_nc:
        return False
    try:
        p = os.path.normcase(os.path.abspath(path))
        base = os.path.normcase(os.path.abspath(root_nc))
    except Exception:
        return False
    if p == base:
        return True
    return p.startswith(base.rstrip("/\\" + os.sep) + os.sep)


def _clean_paths(raw):
    """把引擎回传的记录面洗成干净路径列表：单 str 包成 list，None/空白项剔除。

    `str(None)` 是 "None"，不洗掉就会被当成一条真实路径记进 missing——宁缺勿假。
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = [raw]
    out = []
    for x in (raw or []):
        s = _clip(x, 1024).strip()
        if s and s.lower() != "none":
            out.append(s)
    return out


def _import_record(obj):
    """读导入记录里的原始文件绝对路径：两条导入管线都要接住。

    Interchange(5.3+ 默认) -> InterchangeAssetImportData.extract_filenames()；
    旧管线 -> AssetImportData 子类的 get_first_filename() / filenames 属性。
    返回 (files, via, note)：files 为空则 via=None；本函数绝不抛（调用方按 no_record 记账）。
    via 原样回传，跨版本兼容矩阵靠它看哪条签名真的命中。
    """
    errs = []
    si = None
    for prop in ("asset_import_data", "import_data"):
        try:
            cand = obj.get_editor_property(prop)
        except Exception:
            continue
        if cand is not None:
            si = cand
            break
    if si is None:
        return [], None, "no_import_data"
    for meth in _IMPORT_RECORD_METHODS:
        fn = getattr(si, meth, None)
        if not callable(fn):
            continue
        try:
            raw = fn()
        except Exception as exc:
            errs.append("%s:%s" % (meth, _clip(exc, 60)))
            continue
        files = _clean_paths(raw)
        if files:
            return files, meth, ("|".join(errs) if errs else None)
    for prop in _IMPORT_RECORD_PROPS:
        try:
            raw = si.get_editor_property(prop)
        except Exception:
            continue
        files = _clean_paths(raw)
        if files:
            return files, "prop:" + prop, ("|".join(errs) if errs else None)
    note = "empty_record"
    if errs:
        note = note + "|" + "|".join(errs)
    return [], None, _clip(note, 200)


def _src_verdict(recorded):
    """按记录路径的在场情况定档；空记录 -> no_record（不等于源丢失）。"""
    if not recorded:
        return "no_record"
    hits = 0
    for r in recorded:
        if r["exists"]:
            hits += 1
    if hits == len(recorded):
        return "present"
    if hits == 0:
        return "missing"
    return "partial"


def _source_rollup(rows, by_dir_cap=30, max_items=200):
    """把扫描行汇成五档计数、受影响体量与目录聚合，并按『缺料优先』定行序。

    纯 stdlib（不碰 unreal），L0 直接钉聚合与截断口径。missing_bytes 只计 verdict=missing 的
    uasset 磁盘体量（可维护性口径，不是显存/池金额）；partial 单独计数，两本账不合并。
    """
    verdict_counts = {}
    by_dir = {}
    for r in rows:
        v = r.get("verdict")
        verdict_counts[v] = verdict_counts.get(v, 0) + 1
        size = int(r.get("uasset_bytes") or 0)
        parent = r["package"].rpartition("/")[0] or "/"
        slot = by_dir.setdefault(parent, {"count": 0, "missing": 0, "partial": 0,
                                          "bytes": 0, "missing_bytes": 0})
        slot["count"] += 1
        slot["bytes"] += size
        if v == "missing":
            slot["missing"] += 1
            slot["missing_bytes"] += size
        elif v == "partial":
            slot["partial"] += 1

    present = verdict_counts.get("present", 0)
    partial = verdict_counts.get("partial", 0)
    missing = verdict_counts.get("missing", 0)
    missing_bytes = sum(int(r["uasset_bytes"] or 0) for r in rows if r["verdict"] == "missing")

    dirs_sorted = sorted(by_dir.items(),
                         key=lambda kv: (-kv[1]["missing_bytes"], -kv[1]["count"], kv[0]))
    out = {
        "present_count": present,
        "partial_count": partial,
        "missing_count": missing,
        "no_record": verdict_counts.get("no_record", 0),
        "load_failed_count": verdict_counts.get("load_failed", 0),
        "with_record": present + partial + missing,
        "missing_bytes": missing_bytes,
        "missing_mb": round(missing_bytes / 1048576.0, 3),
        "by_dir": {k: {"count": v["count"], "missing": v["missing"],
                       "partial": v["partial"], "bytes": v["bytes"],
                       "missing_bytes": v["missing_bytes"],
                       "missing_mb": round(v["missing_bytes"] / 1048576.0, 3)}
                   for k, v in dirs_sorted[:by_dir_cap]},
        "by_dir_truncated": len(dirs_sorted) > by_dir_cap,
    }
    order = {"missing": 0, "partial": 1, "load_failed": 2, "no_record": 3, "present": 4}
    rows = sorted(rows, key=lambda r: (order.get(r["verdict"], 5),
                                       -int(r["uasset_bytes"] or 0), r["package"]))
    if max_items and max_items > 0:
        out["rows"] = rows[:max_items]
        out["rows_truncated"] = len(rows) > max_items
    else:
        out["rows"] = rows
        out["rows_truncated"] = False
    return out


@register
def scan_missing_source_files(folder="/Game", class_contains="", limit=200,
                              offset=0, max_items=200):
    """诊断『导入原文件已不在记录路径上』的资产（只读）：这类资产改尺寸/改压缩/重导入都无从下手。

    UE 导入时把外部原文件的绝对路径定格在 asset_import_data 里。本工具枚举 folder 下资产，
    读该记录并逐个判本机是否还在场，输出缺失清单 + 按资产目录聚合：
      present     记录的文件全在场（还能重导入；可继续读源文件头与运行时估算互验）
      partial     部分在场（多文件源缺料，重导入会失败或不完整）
      missing     全不在场（源已丢，只能在引擎侧钳 MaxSize/改 LOD —— 本工具的主目标）
      no_record   没有导入记录（编辑器内创作、代码生成、或旧管线未记录；**不等于源丢失**）
      load_failed 资产载入失败（损坏/依赖缺失），计入 load_failed_count
    verdict 之外的 missing_bytes/missing_mb 只是**受影响资产的 uasset 磁盘体量**（可维护性口径），
    不是省显存/省池的金额——源文件本来就不进 cook 产物，删它一分钱收益都没有。

    诚实边界（务必读）：
    - 记录路径是**导入那一刻的定格**，可能属于别人的机器、别的盘符或已拔掉的外置盘；
      「missing」= 本机找不到源，不代表美术没留档（可能换机器、归档网盘）。据此删资产是错的。
    - 本工具只读，但 `unreal.load_asset` 会把窗口内的资产**载入编辑器内存**（与 get_asset_metrics
      同类副作用），limit 控窗口大小；大工程先按 class_contains 缩面再翻页。
    - 排除项见 skipped 计数（关卡/外部 Actor/重定向桩），它们没有外部源概念，不计入清单。

    folder 为 Content 下目录前缀（/Game 前缀可省）；class_contains 按类名子串过滤（如 Texture）；
    行按 uasset 体量降序取 offset/limit 窗口，rows 再截到 max_items。列表带 total/truncated/cap。
    无引擎（总线降级）结构键常驻、返回空。
    """
    folder = (folder or "/Game").strip().strip('"')
    if not folder.startswith("/Game"):
        folder = "/Game/" + folder.lstrip("/")
    class_contains = (class_contains or "").strip().lower()
    try:
        limit = max(1, int(limit))
    except Exception:
        limit = 200
    try:
        offset = max(0, int(offset))
    except Exception:
        offset = 0
    try:
        max_items = max(1, int(max_items))
    except Exception:
        max_items = 200

    result = {
        "folder": folder, "class_contains": class_contains or None,
        "found_registry": False,
        "total_available": 0, "scanned": 0, "offset": offset, "cap": limit,
        "truncated": False,
        "rows": [], "rows_truncated": False,
        "with_record": 0, "no_record": 0,
        "present_count": 0, "partial_count": 0, "missing_count": 0,
        "load_failed_count": 0,
        "missing_bytes": 0, "missing_mb": 0.0,
        "skipped": {}, "skipped_count": 0,
        "by_dir": {}, "by_dir_truncated": False,
        "api": {"import_record_via": {}},
        "note": ("verdict 口径：present/partial/missing 按 asset_import_data 记录路径的本机在场性；"
                 "no_record=编辑器内创作或旧管线未记录（不等于源丢失）。记录路径是导入时定格，"
                 "可能属他人机器/别盘符，missing 不可据此删资产。missing_mb 是受影响 uasset 体量，"
                 "不是显存/池金额。"),
    }
    try:
        import unreal
    except Exception:
        result["note"] = "no_engine: 需编辑器在线(桥)才能读导入记录并判原文件在场性。"
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

    root_nc = _project_dir_nc(unreal)
    skipped = {}
    entries = []
    for pkg, d in _dedupe_by_package(datas, unreal):
        cls = _class_name(unreal, d) or ""
        if class_contains and class_contains not in cls.lower():
            continue
        ex = _src_scan_skip(cls, pkg)
        if ex:
            skipped[ex] = skipped.get(ex, 0) + 1
            continue
        _fp, size = _disk_size(unreal, pkg)
        entries.append((pkg, cls, int(size) if size else 0))
    result["skipped"] = skipped
    result["skipped_count"] = sum(skipped.values())
    entries.sort(key=lambda t: (-t[2], t[0]))

    total_all = len(entries)
    result["total_available"] = total_all
    window = entries[offset:offset + limit]
    result["scanned"] = len(window)
    result["truncated"] = (offset + limit) < total_all

    rows = []
    via_counts = {}
    load_failed_examples = []
    for pkg, cls, size in window:
        row = {"package": pkg, "class": cls, "uasset_bytes": size,
               "uasset_mb": round(size / 1048576.0, 3),
               "verdict": None, "via": None, "recorded": []}
        obj = None
        try:
            obj = unreal.load_asset(pkg)
        except Exception as exc:
            obj = None
            if len(load_failed_examples) < 10:
                load_failed_examples.append("%s: %s" % (pkg, _clip(exc, 90)))
        if obj is None:
            row["verdict"] = "load_failed"
        else:
            files, via, rec_note = _import_record(obj)
            rec = []
            for f in files:
                try:
                    exists = bool(os.path.isfile(f))
                except Exception:
                    exists = False
                rec.append({"path": f.replace("\\", "/"), "exists": exists,
                            "under_project": _is_under(f, root_nc),
                            "ext": os.path.splitext(f)[1].lower()})
            row["recorded"] = rec
            row["via"] = via
            row["verdict"] = _src_verdict(rec)
            if rec_note:
                row["record_note"] = rec_note
            if via:
                via_counts[via] = via_counts.get(via, 0) + 1
            obj = None
        rows.append(row)

    result["api"]["import_record_via"] = via_counts
    for key, value in _source_rollup(rows, by_dir_cap=30, max_items=max_items).items():
        result[key] = value

    if result["no_record"] and result["with_record"] == 0 and result["load_failed_count"] == 0:
        result["note"] = result["note"] + (
            " 本窗 %d 个资产全无导入记录——多半是编辑器内创作/旧管线未定格，"
            "不等于源文件丢失，别按缺失处理。" % result["scanned"])
    if load_failed_examples:
        result["note"] = result["note"] + " 载入失败样例见 load_failed_examples。"
        result["load_failed_examples"] = load_failed_examples[:10]
    if result["truncated"]:
        result["scan_note"] = ("本窗仅检查 offset=%d 起 %d 个资产(共 %d，按 uasset 体量降序)；"
                              "计数与 by_dir 为该窗口内值，翻页可累计。"
                              % (offset, result["scanned"], total_all))
    return result
