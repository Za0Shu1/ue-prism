"""opt_/fix_ 系列共用的领域小工具（domain 层；Python 3.7 子集；只用 unreal + 标准库）。

写操作铁律落位在这里，避免每个 opt_ 函数重复实现：
- 双钥自守卫：dry_run=False 必须配 confirm=True，否则结构化错误（server 层另有归一前置）；
- 跨版本 API 漂移：属性读写一律多候选名 + tried 证据链，全败记 UE_API_MISMATCH；
- 无引擎（总线降级测试）：结构键常驻、executed=False、note=no_engine，绝不假装成功；
- 目标解析三选一：显式 paths > map 依赖闭包 > folder 枚举，统一按磁盘体量降序开窗。

模板参照 migrate.py（_try_op / _no_engine），只读采集参照 metrics.py。
"""
from __future__ import annotations

import re

from .. import envelope
from . import assets as _a

# 参与优化的类名关键词（注册表 asset_class_path 末级名，大小写不敏感子串）
TEXTURE_TERMS = ("texture",)
MESH_TERMS = ("staticmesh",)

# Nanite 不支持的半透明混合模式（5.x 一致：Nanite 网格挂这些材质会静默降级 + 刷日志）
# 5.4 实证 unreal.BlendMode 成员：OPAQUE / MASKED / TRANSLUCENT / ADDITIVE / MODULATE /
# ALPHA_COMPOSITE / ALPHA_HOLDOUT / TRANSLUCENT_COLORED_TRANSMITTANCE。引擎只放行前两个
# （LogStaticMesh: "... Only opaque or masked blend modes are currently supported"）。
NANITE_SUPPORTED_BLENDS = ("opaque", "masked")
NANITE_UNSUPPORTED_BLENDS = ("translucent", "translucentcoloredtransmittance",
                             "additive", "modulate", "alphacomposite", "alphaholdout")
TRANSLUCENT_BLENDS = NANITE_UNSUPPORTED_BLENDS   # 兼容旧引用名

# 材质槽元素字段：5.4 实证 FStaticMaterial 只有 material_interface / material_slot_name，
# material 这个字段名在 Python 侧不存在（旧版本/文档偶见，留作候选）。
SLOT_MATERIAL_KEYS = ("material_interface", "material")

_TRUE = ("1", "true", "yes", "y", "on")
_FALSE = ("", "0", "false", "no", "n", "off")


def to_bool(value, default=False):
    """总线/MCP 可能把 bool 送成字符串：显式归一，未知值走 default 并让调用方记证据。"""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    return default


def as_path_list(value):
    if not value:
        return []
    if isinstance(value, (list, tuple)):
        seq = list(value)
    else:
        seq = re.split(r"[,\n;]+", str(value))
    out = []
    for item in seq:
        text = str(item).strip()
        if text:
            out.append(text)
    return out


def double_key_guard(op, dry_run, confirm):
    """执行态(非 dry_run)必须 confirm=True；否则回结构化错误（不写、不崩）。"""
    if to_bool(dry_run, True):
        return None
    if not to_bool(confirm, False):
        return envelope.make_err(envelope.Code.RUNTIME_ERROR,
                                 "%s 是写操作：需 dry_run=False 且 confirm=True（双钥）" % op)
    return None


def no_engine(op, extra=None):
    """无引擎降级：结构键常驻 + executed=False，调用方据此判"没做"而不是"没东西做"。"""
    base = {"op": op, "dry_run": True, "executed": False, "success": False,
            "note": "no_engine", "targets": [], "universe": 0, "truncated": False,
            "cap": 0, "changed": 0, "skipped": [], "failed": [], "tried": []}
    if extra:
        for k in extra:
            base[k] = extra[k]
    return envelope.make_ok(base)


def _class_match(cls_name, terms):
    key = str(cls_name or "").lower()
    if not key:
        return False
    for term in terms:
        if term in key:
            return True
    return False


def _from_registry(unreal, ar, folder, terms, limit, offset):
    """folder 递归枚举 + 磁盘体量降序开窗。返回 (targets, universe, note)。"""
    datas = _a._assets_under(ar, unreal, folder or "/Game")
    if datas is None:
        return None, 0, "get_assets_by_path 无候选签名命中（5.0-5.8 漂移？）"
    # 一包一条：AssetRegistry 对多导出包（HLOD 烘焙图集/子资产）按导出返回多条，
    # 不去重会让同包挤占 limit 窗口并重复度量（5.4 真实工程实测一个 HLOD 包占 48 格）。
    rows = []
    for pkg, data in _a._dedupe_by_package(datas, unreal, prefer_terms=terms):
        cls = _a._class_name(unreal, data)
        if not _class_match(cls, terms):
            continue
        _fp, size = _a._disk_size(unreal, pkg)
        rows.append({"package": pkg, "class": cls, "disk_bytes": int(size or 0),
                     "object_path": _a._export_object_path(unreal, pkg, data)})
    rows.sort(key=lambda r: (-r["disk_bytes"], r["package"]))
    universe = len(rows)
    window = rows[max(0, int(offset)):max(0, int(offset)) + int(limit)]
    return window, universe, ""


def _from_map(unreal, ar, map_path, terms, limit, offset):
    """按关卡 uses 闭包取纹理/网格（"这张图到底被不被这张图用"才有池意义）。"""
    chain = _a.get_asset_chain(map_path, direction="uses", scope="game",
                               with_meta=True, max_nodes=4000)
    if isinstance(chain, dict) and chain.get("ok") is False:
        return None, 0, chain.get("error")
    rows = []
    for node in chain.get("nodes") or []:
        if not _class_match(node.get("class"), terms):
            continue
        pkg = node.get("package")
        if not pkg:
            continue
        leaf = pkg.rpartition("/")[2]
        rows.append({"package": pkg, "class": node.get("class"),
                     "disk_bytes": int(node.get("size_bytes") or 0),
                     "object_path": pkg + "." + leaf})
    rows.sort(key=lambda r: (-r["disk_bytes"], r["package"]))
    universe = len(rows)
    return rows[max(0, int(offset)):max(0, int(offset)) + int(limit)], universe, ""


def _from_paths(paths, terms, limit, offset, unreal, ar):
    rows = []
    skipped = []
    for raw in paths:
        package, object_path, _leaf = _a._normalize(raw)
        data = _a._asset_data(ar, unreal, package, object_path) if ar is not None else None
        if data is None:
            skipped.append({"query": raw, "note": "asset_not_found_in_registry"})
            continue
        cls = _a._class_name(unreal, data)
        if not _class_match(cls, terms):
            skipped.append({"query": package, "note": "class_mismatch", "class": cls})
            continue
        _fp, size = _a._disk_size(unreal, package)
        rows.append({"package": package, "class": cls, "object_path": object_path,
                     "disk_bytes": int(size or 0)})
    rows.sort(key=lambda r: (-r["disk_bytes"], r["package"]))
    universe = len(rows)
    return rows[max(0, int(offset)):max(0, int(offset)) + int(limit)], universe, "", skipped


def resolve_targets(paths=None, folder=None, map_path=None, terms=TEXTURE_TERMS,
                    limit=200, offset=0):
    """统一目标解析（只读）。返回 dict(targets, universe, truncated, cap, source, note,
    found_registry, rejected)。无引擎时 found_registry=False + note=no_engine。"""
    try:
        limit = max(1, int(limit))
    except (TypeError, ValueError):
        limit = 200
    try:
        offset = max(0, int(offset))
    except (TypeError, ValueError):
        offset = 0
    out = {"targets": [], "universe": 0, "truncated": False, "cap": limit,
           "source": None, "note": None, "found_registry": False, "rejected": []}
    try:
        import unreal
    except Exception:
        out["note"] = "no_engine"
        return out
    ar = _a._registry()
    if ar is None:
        out["note"] = "get_asset_registry 不可用"
        return out
    out["found_registry"] = True
    listed = as_path_list(paths)
    skipped = []
    if listed:
        rows, universe, err, skipped = _from_paths(listed, terms, limit, offset, unreal, ar)
        out["source"] = "paths"
    elif map_path:
        rows, universe, err = _from_map(unreal, ar, map_path, terms, limit, offset)
        out["source"] = "map"
    else:
        rows, universe, err = _from_registry(unreal, ar, folder or "/Game", terms, limit, offset)
        out["source"] = "folder"
    if rows is None:
        out["note"] = err or "target resolution failed"
        out["rejected"] = skipped[:20]
        return out
    out["targets"] = rows
    out["universe"] = universe
    out["truncated"] = universe > len(rows)
    if err:
        out["note"] = err
    out["rejected"] = skipped[:20]
    return out


def load_object(unreal, object_path):
    """EditorAssetLibrary(需插件) / load_asset(核心) 两路兜底（同 metrics 口径）。"""
    for getter in (lambda: unreal.EditorAssetLibrary.load_asset(object_path),
                   lambda: unreal.load_asset(object_path)):
        try:
            obj = getter()
        except Exception:
            continue
        if obj is not None:
            return obj
    return None


def prop(obj, key):
    try:
        return obj.get_editor_property(key)
    except Exception:
        try:
            return getattr(obj, key)
        except Exception:
            return None


def get_prop_any(obj, keys):
    """多候选属性名读取：返回 (值, 命中名, tried)。全 miss -> (None, None, tried)。"""
    tried = []
    for key in keys:
        if obj is None:
            tried.append({"prop": key, "err": "no_object"})
            continue
        value = prop(obj, key)
        if value is None:
            tried.append({"prop": key, "err": "miss"})
            continue
        return value, key, tried
    return None, None, tried


def set_prop_any(obj, keys, value):
    """多候选属性名写入：set_editor_property 优先，退化 setattr。返回 (ok, 命中名, tried)。"""
    tried = []
    for key in keys:
        hit = None
        try:
            obj.set_editor_property(key, value)
            hit = "set_editor_property(%s)" % key
        except Exception as exc:
            tried.append({"sig": "set_editor_property:" + key, "err": type(exc).__name__})
            try:
                setattr(obj, key, value)
                hit = "setattr(%s)" % key
            except Exception as exc2:
                tried.append({"sig": "setattr:" + key, "err": type(exc2).__name__})
        if hit:
            # 写回读校验：UE 有时静默忽略（属性只读/需重建），不校验就是假成功
            confirm_value = prop(obj, key)
            if _same_value(confirm_value, value):
                return True, hit, tried
            tried.append({"sig": "verify:" + key, "err": "value_not_applied",
                          "got": str(confirm_value)[:60]})
            continue
    return False, None, tried


def _same_value(got, want):
    if got is None:
        return False
    if isinstance(want, bool):
        return bool(got) is want
    try:
        return str(got).strip().lower() == str(want).strip().lower()
    except Exception:
        return got == want


def save_asset(unreal, package, obj=None):
    """保存资产（属性改动是内存态，不保存就丢）。多候选 + tried，取不到不假装已存。"""
    tried = []
    E = getattr(unreal, "EditorAssetLibrary", None)
    if E is None:
        return False, None, [{"sig": "unreal.EditorAssetLibrary", "err": "AttributeMissing"}]
    for call, sig in (
        (lambda: E.save_asset(package), "save_asset(package)"),
        (lambda: E.save_asset(package, False), "save_asset(package, only_if_is_dirty=False)"),
    ):
        try:
            ret = call()
        except Exception as exc:
            tried.append({"sig": sig, "err": type(exc).__name__})
            continue
        tried.append({"sig": sig, "ret": ret})
        if ret is not False:
            return True, sig, tried
    if obj is not None:
        try:
            ret = E.save_loaded_asset(obj)
            tried.append({"sig": "save_loaded_asset(obj)", "ret": ret})
            if ret is not False:
                return True, "save_loaded_asset(obj)", tried
        except Exception as exc:
            tried.append({"sig": "save_loaded_asset(obj)", "err": type(exc).__name__})
    return False, None, tried


def _static_material_slots(obj, tried):
    """static_materials -> (材质列表, 槽位数, 命中签名)。5.4 实证的主路径。"""
    sm = prop(obj, "static_materials")
    if sm is None:
        tried.append({"sig": "prop:static_materials", "err": "miss"})
        return [], 0, None
    rows = list(sm)
    mats = []
    for slot in rows:
        value, _key, _sub = get_prop_any(slot, SLOT_MATERIAL_KEYS)
        if value is not None:
            mats.append(value)
    if rows and not mats:
        tried.append({"sig": "prop:static_materials", "ret": 0,
                      "note": "%d 个槽位但 material_interface/material 都取不到" % len(rows)})
    return mats, len(rows), ("prop:static_materials" if mats else None)


def _section_materials(obj, tried):
    """get_material(section_index) x get_num_sections(0)：无 static_materials 时的兜底。"""
    getter = getattr(obj, "get_material", None)
    counter = getattr(obj, "get_num_sections", None)
    if getter is None or counter is None:
        tried.append({"sig": "get_material(i)", "err": "AttributeMissing"})
        return [], None
    try:
        sections = int(counter(0) or 0)
    except Exception as exc:
        tried.append({"sig": "get_num_sections(0)", "err": type(exc).__name__})
        return [], None
    if sections <= 0:
        tried.append({"sig": "get_num_sections(0)", "ret": 0})
        return [], 0, None
    mats = []
    for index in range(sections):
        try:
            value = getter(index)
        except Exception as exc:
            tried.append({"sig": "get_material(%d)" % index, "err": type(exc).__name__})
            continue
        if value is not None:
            mats.append(value)
    return mats, sections, ("get_material(i)" if mats else None)


def mesh_materials(unreal, obj):
    """网格材质槽里的材质对象。返回 (mats, api, tried, unreadable)。

    5.4 真机实证（scripts/probe_material_api.py）：StaticMesh 只有 static_materials
    （元素字段 material_interface）与 get_material(index)；get_lod_used_materials /
    get_used_materials / render_materials 在 5.4 的 Python 侧根本不存在。旧实现只试
    这三个 + 元素上的 material 字段，结果恒返空列表，但 blend_unreadable 仍是 false
    —— "Nanite + 半透明"拦截形同虚设。unreadable=True 表示"槽位在、材质取不到"，
    上层必须按未知保守处理（不开、也不关）。
    """
    tried = []
    mats, slots, hit = _static_material_slots(obj, tried)
    if mats:
        return mats, hit, tried, slots > len(mats)
    if slots:
        return [], None, tried, True     # 有槽位却取不到材质：判读不了，不等于"没有材质"
    for call, sig in (
        (lambda: list(obj.get_lod_used_materials(0)), "get_lod_used_materials(0)"),
        (lambda: [m for m in (prop(obj, "render_materials") or []) if m is not None],
         "prop:render_materials"),
    ):
        try:
            found = call()
        except Exception as exc:
            tried.append({"sig": sig, "err": type(exc).__name__})
            continue
        if found:
            return found, sig, tried, False
        tried.append({"sig": sig, "ret": 0})
    mats, sections, hit2 = _section_materials(obj, tried)
    if mats:
        return mats, hit2, tried, False
    # 有 section 却一个材质都取不到 = 判读不了（保守），不是"没有材质"
    return [], None, tried, sections > 0


def base_material(unreal, material):
    """材质接口 -> 底层基类材质：返回 (对象, 命中签名, tried)。

    5.4 实证：static_materials[i].material_interface 取回的是 MaterialInterface，而
    used_with_nanite 这类开关挂在 UMaterial 上 —— 在接口上读恒 miss，真机表现就是
    材质清单一律 used_with_nanite=null。get_base_material() 与 prop:base_material
    两路兜底；都拿不到就原样返回，调用方按"读不到"降级，不猜值。
    """
    tried = []
    getter = getattr(material, "get_base_material", None)
    value = None
    if callable(getter):
        try:
            value = getter()
        except Exception as exc:
            tried.append({"sig": "get_base_material()", "err": type(exc).__name__})
        if value is not None:
            tried.append({"sig": "get_base_material()", "ret": "hit"})
            return value, "get_base_material()", tried
        tried.append({"sig": "get_base_material()", "ret": "None"})
    else:
        tried.append({"sig": "get_base_material()", "err": "AttributeMissing"})
    value = prop(material, "base_material")
    if value is not None:
        tried.append({"sig": "prop:base_material", "ret": "hit"})
        return value, "prop:base_material", tried
    tried.append({"sig": "prop:base_material", "err": "miss"})
    return material, None, tried


def blend_key(value):
    """UE BlendMode 枚举 -> 归一关键字。

    5.4 实证 str(枚举) 形如 '<BlendMode.BLEND_TRANSLUCENT: 1>'，取成员名去掉 blend_ 前缀
    并抹平下划线，避免 "alphacomposite" 这类写法永远匹配不上 "BLEND_ALPHA_COMPOSITE"。
    """
    text = str(value or "").strip().lower()
    if not text:
        return ""
    core = text.strip("<>").split(":")[0].strip()
    core = core.rpartition(".")[2]
    return core.replace("blend_", "").replace("_", "")


def blend_mode_of(unreal, material, _retry=True):
    """材质混合模式 -> (归一名, Nanite 是否不支持, 命中签名, 是否可判读)。

    5.4 实证：get_blend_mode() 挂在 MaterialInterface 基类上（材质实例同样可直读），
    Material 另有 blend_mode 属性；MaterialFunction 两者都没有 -> 判读不了。
    取不到或枚举认不出一律 readable=False，让上层按"未知"保守处理。绝不拿 two_sided
    这类布尔兜底：那会把"读不到"伪装成"已读且安全"，半透明拦截就又形同虚设了。
    """
    tried = []
    text = None
    hit = None
    for sig, call in (("get_blend_mode()", lambda: material.get_blend_mode()),
                      ("prop:blend_mode", lambda: prop(material, "blend_mode"))):
        try:
            value = call()
        except Exception as exc:
            tried.append({"sig": sig, "err": type(exc).__name__})
            continue
        if value is not None:
            text, hit = str(value), sig
            break
        tried.append({"sig": sig, "ret": "None"})
    if text is None:
        # 接口上读不到（个别版本 / MaterialFunction）才解包基类材质再试一次
        if _retry:
            base, _bapi, _btried = base_material(unreal, material)
            if base is not None and base is not material:
                return blend_mode_of(unreal, base, False)
        return None, False, None, False
    key = blend_key(text)
    name = key or text.lower()      # 认得出就报干净名（translucent），认不出保留原文供排查
    if key in NANITE_UNSUPPORTED_BLENDS:
        return name, True, hit, True
    if key in NANITE_SUPPORTED_BLENDS:
        return name, False, hit, True
    return name, False, hit, False


def _to_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _acc_savings(base, decision):
    """收益分两本账，别把"移出池"和"释放常驻"混成一个数字。

    inspect 给 permanent_release_mb 非空 -> 计常驻释放（禁流送/无 mip 系）；
    否则计池/显存节省（钳尺寸、虚拟化、改压缩）。
    """
    value = _to_float(decision.get("savings_mb"))
    if decision.get("permanent_release_mb"):
        base["est_permanent_release_mb"] += value
    else:
        base["est_savings_mb"] += value


def drive(op, terms, paths, folder, map_path, limit, offset, dry_run, confirm,
          inspect_fn, write_fn, extra=None, model=None, list_cap=80):
    """写操作通用骨架（domain 层自守卫，server 层另有 _as_bool 归一前置）。

    流程：双钥守卫 -> 解析目标 -> 逐项 inspect -> dry-run 出计划 / 执行则 write+save
    -> 诚实计数。kind 语义：change=待改/已改；no_op=已达标；skip=明确不该动(带原因)；
    其它/异常 -> failed 并带 tried。changed 只统计"写入且回读校验通过"的项。
    """
    guard = double_key_guard(op, dry_run, confirm)
    if guard is not None:
        return guard
    dry = to_bool(dry_run, True)
    resolved = resolve_targets(paths=paths, folder=folder, map_path=map_path, terms=terms,
                               limit=limit, offset=offset)
    base = {"op": op, "dry_run": dry, "executed": False, "success": False,
            "scope": {"folder": folder, "map": map_path, "source": resolved["source"]},
            "targets": [], "changed": 0, "no_op": 0, "skipped": [], "failed": [],
            "planned": 0, "est_savings_mb": 0.0, "est_permanent_release_mb": 0.0,
            "universe": resolved["universe"],
            "total": 0, "truncated": resolved["truncated"], "cap": resolved["cap"],
            "note": resolved["note"], "rejected": resolved["rejected"], "tried": []}
    if model:
        base["model"] = model
    if extra:
        for key in extra:
            base[key] = extra[key]
    if not resolved["found_registry"]:
        base["note"] = resolved["note"] or "no_engine"
        return envelope.make_ok(base)

    import unreal
    tried = []
    rows = []
    for target in resolved["targets"]:
        obj = load_object(unreal, target["object_path"])
        if obj is None:
            rows.append({"package": target["package"], "class": target["class"],
                         "kind": "failed", "note": "load_failed"})
            continue
        decision = inspect_fn(unreal, obj, target, tried) or {}
        kind = decision.get("kind")
        row = {"package": target["package"], "class": target["class"], "kind": kind,
               "before": decision.get("before"), "after": decision.get("after"),
               "savings_mb": decision.get("savings_mb")}
        for key in ("reason", "source_width", "current_size", "runtime_mb", "triangles",
                    "materials", "translucent", "detail", "format_class",
                    "permanent_release_mb"):
            if decision.get(key) is not None:
                row[key] = decision[key]
        if kind == "no_op":
            base["no_op"] += 1
            rows.append(row)
            continue
        if kind == "skip":
            base["skipped"].append(row)
            rows.append(row)
            continue
        if kind != "change":
            row["note"] = decision.get("note") or "判定失败"
            row["tried"] = (decision.get("tried") or tried)[-6:]
            base["failed"].append(row)
            rows.append(row)
            continue
        if dry:
            base["planned"] += 1
            _acc_savings(base, decision)
            rows.append(row)
            continue
        ok, api, sub_tried = write_fn(unreal, obj, decision, target)
        row["api"] = api
        if not ok:
            row["note"] = decision_write_note(op)
            row["tried"] = (sub_tried or [])[-6:]
            base["failed"].append(row)
            rows.append(row)
            continue
        saved, save_api, save_tried = save_asset(unreal, target["package"], obj)
        row["saved"] = saved
        row["save_api"] = save_api
        base["changed"] += 1
        _acc_savings(base, decision)
        if not saved:
            row["note"] = "已写入内存但保存失败：改动会随编辑器关闭丢失，请在编辑器手动保存"
            row["save_tried"] = (save_tried or [])[-4:]
        rows.append(row)

    base["executed"] = not dry
    base["targets"] = rows[:list_cap]
    base["targets_truncated"] = len(rows) > list_cap
    base["total"] = len(rows)
    base["est_savings_mb"] = round(base["est_savings_mb"], 3)
    base["est_permanent_release_mb"] = round(base["est_permanent_release_mb"], 3)
    base["tried"] = tried[-40:]
    if dry:
        base["note"] = ("dry-run 计划：将改 %d 项（估算收益见各行 savings_mb：池/显存 %sMB"
                        "%s），已达标 %d，跳过 %d，失败 %d。"
                        "确认后带 dry_run=False 且 confirm=True 执行。"
                        % (base["planned"], base["est_savings_mb"],
                           ("，释放常驻 %sMB" % base["est_permanent_release_mb"])
                           if base["est_permanent_release_mb"] else "",
                           base["no_op"], len(base["skipped"]), len(base["failed"])))
    else:
        saved_count = 0
        for r in rows:
            if r.get("saved"):
                saved_count += 1
        base["success"] = base["changed"] > 0 and not base["failed"]
        base["note"] = ("已改 %d 项（成功保存 %d 项），已达标 %d，跳过 %d，失败 %d。"
                        "属性改动需编辑器重建/重压缩才反映到盘上；复跑对应只读工具核对。"
                        % (base["changed"], saved_count, base["no_op"],
                           len(base["skipped"]), len(base["failed"])))
    return envelope.make_ok(base)


def decision_write_note(op):
    return "属性未写入（跨版本签名漂移或只读）：见 tried 证据"
