"""迁移执行器（v1.0 PR-4 · 本仓库首个写操作）。经 bridge 在编辑器内执行。

写操作铁律（AGENTS / REQUIREMENTS §2）：
- 函数名以 migrate_ 前缀；要求 confirm=True 才真正落盘（server 层另有 dry_run 双钥前置）；
- 跨版本 EditorAssetLibrary 签名漂移 -> 多候选 + tried 证据链；整体不中报 UE_API_MISMATCH；
- 无引擎（总线降级测试）返回 executed=False + note=no_engine，绝不假装成功。

改名/移动走 EditorAssetLibrary -> 由 UE 自动修复指向目标资产的所有引用（改动在内存，需用户自行保存/提交 VCS，并用 get_asset_references 复核）。
"""
from __future__ import annotations

from .. import envelope
from . import assets as _a
from . import register


def _no_engine(op, old, new):
    return envelope.make_ok({
        "op": op, "old": old, "new": new,
        "executed": False, "success": False, "note": "no_engine",
    })


def _try_op(fn, candidates):
    """逐个尝试 (args_tuple, sig)；返回 (是否成功, 命中签名, tried证据)。"""
    tried = []
    for args, sig in candidates:
        try:
            res = fn(*args)
        except Exception as e:
            tried.append({"sig": sig, "err": type(e).__name__})
            continue
        tried.append({"sig": sig, "ret": res})
        if res is True:
            return True, sig, tried
    return False, None, tried


def _target_exists(unreal, new_object):
    """目标 object path 是否被'真资产'占用；ObjectRedirector（改名遗留桩）视为可覆盖，不算占用。"""
    try:
        ar = _a._registry()
        if ar is None:
            return False
        data = ar.get_asset_by_object_path(unreal.Name(new_object))
        if data is None or (hasattr(data, "is_valid") and not data.is_valid()):
            data = ar.get_asset_by_object_path(new_object)
        if data is None or (hasattr(data, "is_valid") and not data.is_valid()):
            return False
        cls = str(getattr(data, "asset_class_name", "") or "")
        if "ObjectRedirector" in cls:
            return False
        return True
    except Exception:
        return False


def _fixup_redirectors(unreal, old_package, old_leaf):
    """改名/移动后清理旧路径遗留的重定向桩：把引用者直接指到新资产。
    优先 fix_up_redirectors([旧桩对象])，退化到 fix_up_redirectors_in_folder(旧目录)。返回结构化子结果。"""
    E = getattr(unreal, "EditorAssetLibrary", None)
    tried = []
    if E is None:
        return {"attempted": False, "ok": False, "note": "no EditorAssetLibrary"}
    obj = None
    try:
        obj = E.load_asset(old_package)
    except Exception as e:
        tried.append({"sig": "load_asset", "err": type(e).__name__})
    fn = getattr(E, "fix_up_redirectors", None)
    if obj is not None and fn is not None:
        for args, sig in ((([obj],), "fix_up_redirectors([obj])"), ((obj,), "fix_up_redirectors(obj)")):
            try:
                ret = fn(*args)
            except Exception as e:
                tried.append({"sig": sig, "err": type(e).__name__}); continue
            tried.append({"sig": sig, "ret": ret})
            if ret is True:
                return {"attempted": True, "ok": True, "api": sig, "tried": tried}
    fn2 = getattr(E, "fix_up_redirectors_in_folder", None)
    if fn2 is not None:
        folder = old_package.rsplit("/", 1)[0]
        for args, sig in ((([folder],), "fix_up_redirectors_in_folder([folder])"), ((folder,), "fix_up_redirectors_in_folder(folder)")):
            try:
                ret = fn2(*args)
            except Exception as e:
                tried.append({"sig": sig, "err": type(e).__name__}); continue
            tried.append({"sig": sig, "ret": ret})
            if ret is True:
                return {"attempted": True, "ok": True, "api": sig, "tried": tried}
    return {"attempted": True, "ok": any(t.get("ret") is True for t in tried), "tried": tried,
            "note": "未确认清理桩；可在编辑器对该目录 Fix Up Redirectors 手动处理"}


@register
def migrate_asset_rename(asset_path, new_name, confirm=False, fixup_redirectors=True):
    if not confirm:
        return envelope.make_err(envelope.Code.RUNTIME_ERROR,
                                 "migrate_asset_rename 是写操作：需 confirm=True（双钥兜底）")
    package, object_path, _leaf = _a._normalize(asset_path)
    name = str(new_name)
    pkg_dir = package.rsplit("/", 1)[0]
    new_package = pkg_dir + "/" + name
    new_object = new_package + "." + name
    try:
        import unreal
    except Exception:
        return _no_engine("rename", object_path, new_object)
    if _target_exists(unreal, new_object):
        return envelope.make_err(envelope.Code.RUNTIME_ERROR,
                                 "目标已存在，拒绝覆盖: %s" % new_object)
    E = getattr(unreal, "EditorAssetLibrary", None)
    if E is None:
        return envelope.make_err(envelope.Code.UE_API_MISMATCH, "unreal.EditorAssetLibrary 不可用")
    candidates = [
        ((package, new_package), "rename_asset(pkg,pkg)"),
        ((object_path, new_object), "rename_asset(obj,obj)"),
        ((package, new_object), "rename_asset(pkg,obj)"),
    ]
    ok, sig, tried = _try_op(E.rename_asset, candidates)
    if not ok:
        return envelope.make_ok({
            "op": "rename", "old": package, "new": new_package,
            "executed": True, "success": False, "tried": tried,
            "note": "rename_asset 无候选签名返回 True（未确认成功）；请用 describe_asset/get_asset_references 复核后再决定。见 tried 证据。",
        })
    fixup = _fixup_redirectors(unreal, package, _leaf) if fixup_redirectors else None
    note = "改名成功（UE 已改引用，内存态）；"
    if fixup is not None:
        note += "重定向桩清理%s。" % ("已确认" if fixup.get("ok") else "未确认(见 redirectors)")
    else:
        note += "未清理重定向桩(fixup_redirectors=False)。"
    note += "引用者为内存态改动，需保存资产/提交 VCS 后用 get_asset_references 复核。"
    return envelope.make_ok({
        "op": "rename", "old": package, "new": new_package,
        "executed": True, "success": True, "api": sig, "tried": tried,
        "redirectors": fixup, "note": note,
    })


@register
def migrate_asset_move(asset_path, dest_path, new_name=None, confirm=False, fixup_redirectors=True):
    if not confirm:
        return envelope.make_err(envelope.Code.RUNTIME_ERROR,
                                 "migrate_asset_move 是写操作：需 confirm=True（双钥兜底）")
    package, object_path, leaf = _a._normalize(asset_path)
    new_leaf = new_name or leaf
    new_package = str(dest_path).rstrip("/") + "/" + new_leaf
    new_object = new_package + "." + new_leaf
    try:
        import unreal
    except Exception:
        return _no_engine("move", object_path, new_object)
    if _target_exists(unreal, new_object):
        return envelope.make_err(envelope.Code.RUNTIME_ERROR,
                                 "目标已存在，拒绝覆盖: %s" % new_object)
    E = getattr(unreal, "EditorAssetLibrary", None)
    if E is None:
        return envelope.make_err(envelope.Code.UE_API_MISMATCH, "unreal.EditorAssetLibrary 不可用")
    candidates = [
        ((object_path, new_package), "move_asset(object,package)"),
        ((object_path, new_object), "move_asset(object,object)"),
        ((package, new_package), "move_asset(package,package)"),
        ((package, new_object), "move_asset(package,object)"),
    ]
    ok, sig, tried = _try_op(E.move_asset, candidates)
    if not ok:
        return envelope.make_ok({
            "op": "move", "old": object_path, "new": new_object,
            "executed": True, "success": False, "tried": tried,
            "note": "所有候选签名均未返回 True；资产可能未移动。见 tried 证据。",
        })
    fixup = _fixup_redirectors(unreal, package, leaf) if fixup_redirectors else None
    note = "移动成功（UE 已改引用，内存态）；"
    if fixup is not None:
        note += "重定向桩清理%s。" % ("已确认" if fixup.get("ok") else "未确认(见 redirectors)")
    else:
        note += "未清理重定向桩(fixup_redirectors=False)。"
    note += "引用者为内存态改动，需保存资产/提交 VCS 后用 get_asset_references 复核。"
    return envelope.make_ok({
        "op": "move", "old": object_path, "new": new_object,
        "executed": True, "success": True, "api": sig, "tried": tried,
        "redirectors": fixup, "note": note,
    })


def _dup_result(ret):
    """duplicate_asset 返回不一：成功多返回新对象；None/False 视为失败。
    返回 (是否成功, 目标对象路径证据或 None)。"""
    if ret is None or ret is False:
        return False, None
    if ret is True:
        return True, None
    if isinstance(ret, str):
        return (bool(ret), ret if ret else None)
    try:
        p = ret.get_path_name()
    except Exception:
        p = None
    if p:
        return True, _a._name_str(p)
    return True, None


@register
def migrate_asset(asset_path, dest_path, new_name=None, dry_run=True, confirm=False):
    """迁移(A·同工程复制)：把资产 + 其 /Game uses 依赖闭包，按镜像结构复制到 dest_path 目录下。
    默认 dry_run 只回复制计划(不写)。dry_run=False 且 confirm=True 才逐个 duplicate_asset 落盘。
    复制是非破坏性的：原件一律不动。new_name 可给根资源换个名字放置。闭包仅取 /Game（引擎自带依赖不复制）。"""
    ap = str(asset_path or "").strip()
    if not ap.startswith("/Game"):
        return envelope.make_err(envelope.Code.RUNTIME_ERROR, "asset_path 需是 /Game 包路径")
    dest = str(dest_path or "").strip().rstrip("/")
    if not dest.startswith("/Game"):
        return envelope.make_err(envelope.Code.RUNTIME_ERROR, "dest_path 需是 /Game 目录")
    if not dry_run and not confirm:
        return envelope.make_err(envelope.Code.RUNTIME_ERROR,
                                 "migrate_asset 是写操作：需 dry_run=False 且 confirm=True（双钥）")
    try:
        import unreal
    except Exception:
        return envelope.make_ok({"op": "migrate", "mode": "copy", "dry_run": bool(dry_run),
                                 "executed": False, "success": False, "note": "no_engine"})
    package, object_path, leaf = _a._normalize(ap)
    root_name = (str(new_name).strip() if new_name else leaf) or leaf
    chain = _a.get_asset_chain(package, direction="uses", scope="game", with_meta=True, max_nodes=5000, max_depth=0)
    if isinstance(chain, dict) and chain.get("ok") is False:
        return chain
    deps = chain.get("nodes", []) if isinstance(chain, dict) else []
    meta = {}
    members = [package]
    for d in deps:
        p = d.get("package", "")
        if p.startswith("/Game/") and p != package and p not in members:
            members.append(p)
            meta[p] = {"class": d.get("class"), "size_bytes": int(d.get("size_bytes") or 0)}
    meta[package] = {"class": chain.get("root_class"), "size_bytes": int(chain.get("root_size_bytes") or 0)}

    plan_items = []
    collisions = []
    for pkg in members:
        rel = pkg[len("/Game"):]
        head = rel[: rel.rfind("/")] if "/" in rel[1:] else ""
        leaf_name = (root_name if pkg == package else rel.rpartition("/")[2])
        dst = dest + head + "/" + leaf_name
        conflict = _target_exists(unreal, dst + "." + leaf_name)
        if conflict:
            collisions.append(dst)
        plan_items.append({"src": pkg, "dst": dst,
                           "class": meta.get(pkg, {}).get("class"),
                           "size_bytes": meta.get(pkg, {}).get("size_bytes", 0),
                           "root": pkg == package})
    by_class = {}
    total_size = 0
    for it in plan_items:
        c = it.get("class") or "?"
        slot = by_class.setdefault(c, {"count": 0, "size_bytes": 0})
        slot["count"] += 1
        slot["size_bytes"] += it.get("size_bytes", 0)
        total_size += it.get("size_bytes", 0)
    base = {
        "op": "migrate", "mode": "copy", "asset_path": package, "dest_path": dest,
        "packages": len(plan_items), "by_class": by_class,
        "total_size_bytes": total_size, "total_size_mb": round(total_size / 1048576.0, 4),
        "collision_count": len(collisions), "collisions": collisions[:20],
    }
    if dry_run:
        base.update({"dry_run": True, "executed": False,
                     "items": plan_items[:60], "truncated": len(plan_items) > 60,
                     "note": "复制计划(dry-run)。冲突=目标已存在，执行时会整体拒绝(不覆盖)。确认无误带 dry_run=False+confirm=True 执行；原件不受影响。"})
        return envelope.make_ok(base)
    if collisions:
        return envelope.make_err(envelope.Code.RUNTIME_ERROR,
                                 "目标目录存在 %d 个同名冲突，拒绝覆盖(先清理或换 dest): %s" % (len(collisions), ", ".join(collisions[:5])))
    E = getattr(unreal, "EditorAssetLibrary", None)
    if E is None:
        return envelope.make_err(envelope.Code.UE_API_MISMATCH, "unreal.EditorAssetLibrary 不可用")
    dup = getattr(E, "duplicate_asset", None)
    if dup is None:
        return envelope.make_err(envelope.Code.UE_API_MISMATCH, "EditorAssetLibrary.duplicate_asset 不可用")
    results = []
    ok_n = 0
    for it in plan_items:
        entry = {"src": it["src"], "dst": it["dst"]}
        try:
            ret = dup(it["src"], it["dst"])
        except Exception as e:
            entry.update({"ok": False, "err": type(e).__name__})
            results.append(entry)
            continue
        ok, rpath = _dup_result(ret)
        entry["ok"] = ok
        if rpath:
            entry["dst_object"] = rpath
        if ok:
            ok_n += 1
        else:
            entry["note"] = "duplicate_asset 返回 %r" % (ret,)
        results.append(entry)
    base.update({"dry_run": False, "executed": True, "success": (ok_n == len(plan_items) and ok_n > 0),
                 "duplicated": ok_n, "of": len(plan_items), "results": results[:80],
                 "note": "复制完成，原件未改动。副本引用为 UE duplicate 默认(可能与原件共享依赖)；A 阶段先保证安全落盘。"})
    return envelope.make_ok(base)
