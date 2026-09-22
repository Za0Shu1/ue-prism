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
    返回 (nodes[level 从1起], total, truncated, depth_reached, api_sig)。"""
    from collections import deque
    seen = set([root])
    queue = deque([(root, 0)])
    nodes = []
    api_sig = None
    truncated = False
    while queue:
        cur, lvl = queue.popleft()
        if max_depth and lvl >= max_depth:
            continue
        deps, sig = hop(cur)
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
    return nodes, len(nodes), truncated, depth_reached, api_sig


@register
def get_asset_chain(asset_path, direction="uses", scope="game", with_meta=True, max_nodes=2000, max_depth=0):
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
    package, object_path, _leaf = _normalize(asset_path)
    try:
        import unreal
    except Exception:
        return {"root": package, "direction": direction, "scope": scope, "found": False,
                "nodes": [], "total": 0, "truncated": False, "cap": max_nodes, "depth_reached": 0,
                "by_class": {}, "total_size_bytes": 0, "note": "no_engine"}
    ar = _registry()
    if ar is None:
        return envelope.make_err(envelope.Code.UE_API_MISMATCH,
                                 "AssetRegistry unavailable: get_asset_registry failed on this engine build")
    method = "get_dependencies" if direction == "uses" else "get_referencers"

    def hop(pkg):
        return _registry_graph(ar, unreal, method, pkg, False)

    found = _asset_data(ar, unreal, package, object_path) is not None
    raw_nodes, _t, truncated, depth_reached, api_sig = _bfs_closure(hop, package, max_nodes, max_depth)
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
    if scope == "game" and (n_engine or n_script):
        result["note"] = "已过滤引擎/Script 依赖（迁移仅需 /Game）；scope=all 可含全量"
    return result
