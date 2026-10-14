"""纹理流送池：只读诊断 + 批量优化写入（v1.5 PR-1/PR-2；编辑器内执行）。

对应 TODO Feature 1。所有估算口径取自 prism/streaming.py（domain 与 rules 同源，
不会出现"工具说 8GB、报告说 6GB"的两套账）：
- scan_texture_streaming：按 folder / 关卡 uses 闭包 / 显式 paths 枚举纹理，逐张给
  全链运行时估算并归入 池内 / 池外常驻 / 已虚拟化，汇出池需求与三档可执行收益；
- opt_texture_set_virtual / opt_texture_clamp_max_size / opt_texture_set_compression：
  批量写入，默认 dry_run；dry_run=False 且 confirm=True 双钥才落盘。
  Cube/Volume 不支持 VT -> skipped；已达标 -> no_op；属性写不进(跨版本漂移/只读)
  -> failed + tried 证据。绝不把没做成写成成功。

写完需保存资产；MaxSize/压缩改动由编辑器重压缩（下次 cook 生效），用
get_asset_metrics 或本工具复跑核对。只动资产自身属性，不动引用关系。
"""
from __future__ import annotations

from .. import envelope, streaming as _st
from . import metrics as _m
from . import optutil as _u
from . import register


def _int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _flt(value, default):
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _advice_for(entry):
    """把 actions 翻成一句可执行建议（工具输出面向人，规则面向机器）。"""
    bits = []
    if "clamp_max_size" in entry["actions"] and entry.get("clamp_to"):
        bits.append("MaxSize 钳到 %d（省约 %sMB）" % (entry["clamp_to"], entry["clamp_savings_mb"]))
    if "enable_vt" in entry["actions"]:
        bits.append("开虚拟纹理（移出传统池约 %sMB）" % entry["vt_savings_mb"])
    if entry.get("uncompressed"):
        bits.append("未压缩系按 4B/px：压缩格式改 TC_Default（省约 %sMB）"
                    % entry["compression_savings_mb"])
    elif entry.get("high_bpp"):
        bits.append("TC_HDR 高位深不降块压缩（实测 %gB/px，整张 %sMB）：改 TC_Default 约省 %sMB，"
                    "但会丢 HDR 动态范围 —— 属美术决策，默认工具不动，需显式 "
                    "only_uncompressed=False"
                    % (_flt(entry.get("runtime_bytes_per_texel"), 8.0),
                       entry["runtime_mb"], entry["compression_savings_mb"]))
    elif entry.get("non_streaming_format"):
        bits.append("%s 无 mip/禁用流送，整张恒常驻(池外)%sMB：改回 TC_Default 可流送化"
                    % (entry.get("compression"), entry["permanent_mb"]))
    elif "set_compression" in entry["actions"]:
        bits.append("压缩格式改 TC_Default（省约 %sMB）" % entry["compression_savings_mb"])
    if not bits:
        if entry["virtual_texture"]:
            bits.append("已是虚拟纹理，池压力已解")
        elif entry["never_stream"]:
            bits.append("never_stream 常驻池外，需单独评估")
        else:
            bits.append("无需处理")
    if entry.get("vt_reason"):
        bits.append("(" + entry["vt_reason"] + ")")
    return "; ".join(bits)


def _measure(unreal, obj, tried, probe_source=True):
    """载入态度量（副作用仅为已载入资产）；失败返回 None 让上层记 load/unmeasurable。"""
    try:
        return _m._measure_texture(unreal, obj, tried, probe_source=probe_source) or {}
    except Exception:
        return None


def _project_dir(unreal):
    for getter in (lambda: unreal.SystemLibrary.get_project_directory(),
                   lambda: unreal.Paths.project_dir()):
        try:
            value = getter()
        except Exception:
            continue
        if value:
            return str(value)
    return ""


@register
def scan_texture_streaming(folder="/Game", map=None, paths=None, max_assets=200,
                           min_mb=4.0, top=25, px_warn=2048, px_error=4096,
                           offset=0, tight_pct=80.0, min_demand_mb=32.0,
                           probe_source=True):
    """纹理流送池只读诊断（无副作用，除把资产载入编辑器内存）。

    返回：items（按 pool_mb 降序，cap=top）/ summary（streaming.aggregate 全量口径）/
    candidates（need_clamp / need_vt / uncompressed / high_bpp(TC_HDR 高位深，5.4 实测
    运行时 8B/px，只报账不默认改写) / non_streaming / vt_blocked / never_stream）/
    pool（工程 ini 的 PoolSize 候选 + compare_pool 判定）/ sampling（窗口诚实标注）/
    total / truncated / cap / model（估算口径自解释）。
    无引擎时结构键常驻、note=no_engine。

    probe_source=False 关掉源尺寸探针（见 metrics._source_probe 的引擎断言竞态）：
    此时 source_width 退化为运行时有效边长，钳尺寸/超限判定偏保守，sampling.source_probe
    与 note 会显式播报，不会静默给"看着更小"的收益。
    """
    top = max(1, _int(top, 25))
    max_assets = max(1, _int(max_assets, 200))
    min_mb = _flt(min_mb, 4.0)
    px_warn = _int(px_warn, 2048)
    px_error = _int(px_error, 4096)
    probe_source = _m._u_bool(probe_source, True)
    resolved = _u.resolve_targets(paths=paths, folder=folder, map_path=map,
                                  terms=_u.TEXTURE_TERMS, limit=max_assets, offset=offset)
    result = {
        "scope": {"folder": folder, "map": map, "source": resolved["source"],
                  "paths": _u.as_path_list(paths)[:10] if paths else None},
        "items": [], "summary": _st.aggregate([]), "candidates": {
            "need_clamp": [], "need_vt": [], "uncompressed": [], "non_streaming": [],
            "high_bpp": [], "vt_blocked": [], "never_stream": []},
        "pool": None, "sampling": {"universe": resolved["universe"], "measured": 0,
                                   "window": max_assets, "offset": _int(offset, 0)},
        "total": 0, "truncated": False, "cap": top,
        "note": resolved["note"], "tried": [],
        "model": _st.POOL_MODEL,
    }
    if not resolved["found_registry"]:
        result["summary"]["model"] = _st.POOL_MODEL
        return result

    import unreal  # 已确认有引擎（found_registry 分支）
    tried = []
    entries = []
    unmeasured = 0
    for target in resolved["targets"]:
        obj = _u.load_object(unreal, target["object_path"])
        if obj is None:
            unmeasured += 1
            entries.append({"package": target["package"], "class": target["class"],
                            "runtime_mb": 0.0, "actions": [], "flags": ["load_failed"]})
            continue
        metrics = _measure(unreal, obj, tried, probe_source=probe_source)
        if metrics is None:
            unmeasured += 1
            continue
        entry = _st.texture_pool_entry(metrics, target["class"], target["disk_bytes"],
                                       px_error=px_error, px_warn=px_warn)
        entry["package"] = target["package"]
        entry["advice"] = _advice_for(entry)
        entries.append(entry)

    summary = _st.aggregate(entries)
    summary["load_failed"] = unmeasured
    result["summary"] = summary
    result["sampling"]["measured"] = summary["measured"]
    result["sampling"]["source_probe"] = "on" if probe_source else "off"
    if not probe_source:
        result["note"] = ((result.get("note") or "") +
                          " 已关源尺寸探针（避开引擎重压缩断言竞态）：source_width 退化为"
                          "运行时有效边长，钳尺寸收益偏保守。").strip()
    result["tried"] = tried[-40:]

    keep = [e for e in entries
            if max(_flt(e.get("runtime_mb"), 0.0), _flt(e.get("permanent_mb"), 0.0)) >= min_mb
            or (_flt(e.get("clamp_savings_mb"), 0.0) + _flt(e.get("vt_savings_mb"), 0.0)
                + _flt(e.get("compression_savings_mb"), 0.0)) >= min_mb]
    # 排序按"对显存的实际压力"：池内 + 池外常驻，两者都是要花的钱
    keep.sort(key=lambda e: (-(_flt(e.get("pool_mb"), 0.0) + _flt(e.get("permanent_mb"), 0.0)),
                             -_flt(e.get("runtime_mb"), 0.0), str(e.get("package"))))
    result["total"] = len(keep)
    result["truncated"] = len(keep) > top
    result["items"] = keep[:top]

    def _cand(key, rows):
        slim = []
        for e in rows:
            row = {"package": e.get("package"), "runtime_mb": e.get("runtime_mb"),
                   "pool_mb": e.get("pool_mb")}
            if key == "need_clamp":
                row["clamp_to"] = e.get("clamp_to")
                row["savings_mb"] = e.get("clamp_savings_mb")
            elif key == "need_vt":
                row["savings_mb"] = e.get("vt_savings_mb")
            elif key == "uncompressed":
                row["compression"] = e.get("compression")
                row["savings_mb"] = e.get("compression_savings_mb")
            elif key == "non_streaming":
                row["compression"] = e.get("compression")
                row["permanent_mb"] = e.get("permanent_mb")
            elif key == "high_bpp":
                row["compression"] = e.get("compression")
                row["runtime_bytes_per_texel"] = e.get("runtime_bytes_per_texel")
                row["savings_mb"] = e.get("compression_savings_mb")
            elif key == "vt_blocked":
                row["reason"] = e.get("vt_reason")
            slim.append(row)
        slim.sort(key=lambda r: (-_flt(r.get("savings_mb") or r.get("pool_mb"), 0.0),
                                 str(r.get("package"))))
        return slim[:40]

    acts = [e for e in entries if e.get("package")]
    result["candidates"] = {
        "need_clamp": _cand("need_clamp", [e for e in acts if "clamp_max_size" in e["actions"]]),
        "need_vt": _cand("need_vt", [e for e in acts if "enable_vt" in e["actions"]]),
        "uncompressed": _cand("uncompressed", [e for e in acts if e.get("uncompressed")]),
        "high_bpp": _cand("high_bpp", [e for e in acts if e.get("high_bpp")]),
        "non_streaming": _cand("non_streaming",
                               [e for e in acts if e.get("non_streaming_format")]),
        "vt_blocked": _cand("vt_blocked", [e for e in acts if e.get("vt_supported") is False]),
        "never_stream": _cand("never_stream", [e for e in acts if e.get("never_stream")]),
    }

    proj = _project_dir(unreal)
    config = _st.read_pool_config(proj)
    compare = _st.compare_pool(summary["pool_demand_mb"], config["effective_mb"],
                               tight_pct=tight_pct, min_demand_mb=min_demand_mb)
    result["pool"] = {"project_dir": proj or None, "config": config, "compare": compare,
                      "vt_enabled": _st.vt_enabled(config)}
    if compare["verdict"] == "overflow":
        result["note"] = ("池需求(上界) %sMB > 配置池 %sMB：优先按 candidates 降需求，"
                          "而不是继续加大 PoolSize。" % (compare["demand_mb"], compare["pool_mb"]))
    elif compare["verdict"] == "unknown":
        result["note"] = compare["note"]
    return result


# ---- 批量写入（双钥；默认 dry_run 只出计划）----

_LIST_CAP = 80
_COMPRESSION_NAMES = ("TC_DEFAULT", "TC_Default", "DEFAULT", "TC_NORMAL", "TC_MASKS",
                      "TC_EDITORICON", "TC_EditorIcon", "EDITORICON")
_COMPRESSION_ENUMS = ("TextureCompressionSettings", "TextureCompressedMipSettings")


def _drive(op, paths, folder, map_path, limit, offset, dry_run, confirm,
           inspect_fn, write_fn, extra=None, list_cap=80):
    """委托 domain 层通用写操作骨架（optutil.drive），带上池模型口径。"""
    return _u.drive(op, _u.TEXTURE_TERMS, paths, folder, map_path, limit, offset,
                    dry_run, confirm, inspect_fn, write_fn,
                    extra=extra, model=_st.POOL_MODEL, list_cap=list_cap)


def _entry_of(unreal, obj, target, tried, probe_source=True):
    metrics = _measure(unreal, obj, tried,
                       probe_source=_m._u_bool(probe_source, True))
    if metrics is None:
        return None, None
    entry = _st.texture_pool_entry(metrics, target["class"], target.get("disk_bytes"),
                                   px_error=4096, px_warn=2048)
    return metrics, entry


@register
def opt_texture_set_virtual(paths=None, folder="/Game", map=None, limit=200, offset=0,
                            dry_run=True, confirm=False, probe_source=True, list_cap=80):
    """批量开虚拟纹理（写·双钥）。Cube/Volume -> skipped；已虚拟 -> no_op。

    前置条件：工程需启用 VT（RendererSettings/DeviceProfile），本函数只改资产属性，
    不改工程开关；结果里带 vt_enabled_project 供判断（None=未从 ini 读到）。
    """
    def inspect(unreal, obj, target, tried):
        supported, reason = _st.vt_support(target["class"])
        value, hit, sub = _u.get_prop_any(obj, ("virtual_texture_streaming", "is_virtual"))
        before = None if value is None else bool(value)
        if before is None:
            return {"kind": "unknown", "note": "读不到 VT 属性（签名漂移？）",
                    "tried": sub, "before": None, "after": True}
        if not supported:
            return {"kind": "skip", "before": before, "after": before, "reason": reason}
        if before:
            return {"kind": "no_op", "before": True, "after": True,
                    "reason": "已是虚拟纹理（池压力已解）"}
        _metrics, entry = _entry_of(unreal, obj, target, tried, probe_source)
        savings = entry["vt_savings_mb"] if entry else 0.0
        return {"kind": "change", "before": False, "after": True, "prop_hit": hit,
                "savings_mb": savings, "runtime_mb": entry["runtime_mb"] if entry else None}

    def write(unreal, obj, decision, target):
        return _u.set_prop_any(obj, ("virtual_texture_streaming",), True)

    resolved = _u.resolve_targets(paths=paths, folder=folder, map_path=map,
                                  terms=_u.TEXTURE_TERMS, limit=limit, offset=offset)
    vt_flag = None
    if resolved["found_registry"]:
        try:
            import unreal
            vt_flag = _st.vt_enabled(_st.read_pool_config(_project_dir(unreal)))
        except Exception:
            vt_flag = None
    probe = "on" if _u.to_bool(probe_source, True) else "off"
    out = _drive("opt_texture_set_virtual", paths, folder, map, limit, offset,
                 dry_run, confirm, inspect, write,
                 extra={"vt_enabled_project": vt_flag, "source_probe": probe},
                 list_cap=list_cap)
    return out


def _clamp_decision(entry, want, only_if_larger=True, widen=False):
    """钳 MaxTextureSize 的判定（纯函数，离线可测；口径同 scan_texture_streaming）。

    limit = 运行时真正受限的边长 = min(源边长, 现有 MaxSize)。收益/代价按面积平方比折算，
    与 streaming.resize_delta_mb 同一个式子，不会出现"scan 说 79MB、opt 说 0MB"。
    关键守卫：只收紧不放宽——现有 MaxSize 比目标更紧时是 no_op（5.4 真实工程曾把
    T_Dirt 的 512 计划成 2048，那是把限制放大，不是优化）。
    """
    cur = _int(entry.get("max_size"), 0)
    src = _int(entry.get("source_width"), 0)
    judged = src or _int(entry.get("effective_width"), 0)
    limit = _st.effective_px(judged, cur)
    runtime_mb = _flt(entry.get("runtime_mb"), 0.0)
    row = {"source_width": src, "current_size": limit or judged,
           "runtime_mb": runtime_mb, "before": cur or None}
    if widen:
        if cur <= 0:
            return dict(row, kind="no_op", after=None,
                        reason="本来就没设 MaxTextureSize 限制，无需取消")
        growth = _st.resize_delta_mb(runtime_mb, limit, judged)
        return dict(row, kind="change", after=0, savings_mb=growth,
                    reason="max_size=0 取消限制：运行时上限从 %d 变回源尺寸，"
                          "savings_mb 为负即变大量（读不到源尺寸时算不出）" % limit)
    if limit <= 0:
        return dict(row, kind="unknown", after=want,
                    note="读不到尺寸，无法判断是否超限")
    if cur == want:
        return dict(row, kind="no_op", after=cur, reason="max_size 已是 %d" % want)
    if want >= limit and _u.to_bool(only_if_larger, True):
        return dict(row, kind="no_op", after=cur or None,
                    reason="运行时受限边长 %d 已在目标 %d 内：收紧没有收益，"
                          "本工具不把现有 MaxSize 放宽" % (limit, want))
    savings = _st.resize_delta_mb(runtime_mb, limit, want)
    out = dict(row, kind="change", after=want, savings_mb=savings)
    if savings < 0:
        out["reason"] = ("only_if_larger=False 才会走到这里：把运行时上限从 %d 放宽到 %d "
                         "会变大 %sMB（负收益=代价），确认这不是误改" % (limit, want, -savings))
    return out


@register
def opt_texture_clamp_max_size(paths=None, folder="/Game", map=None, max_size=2048,
                               only_if_larger=True, limit=200, offset=0,
                               dry_run=True, confirm=False, probe_source=True, list_cap=80):
    """批量钳 MaxTextureSize（写·双钥）。只对大于目标的生效（only_if_larger）。

    max_size=0 表示"取消限制"，属放大风险动作：only_if_larger 会被忽略并显著提示。
    dry-run 给每张 前→后 尺寸与估算收益（口径同 scan_texture_streaming）。
    """
    want = _int(max_size, 2048)
    if want < 0:
        return envelope.make_err(envelope.Code.RUNTIME_ERROR,
                                 "max_size 需 >=0（0=不限），got %r" % (max_size,))
    widen = want == 0

    def inspect(unreal, obj, target, tried):
        metrics, entry = _entry_of(unreal, obj, target, tried, probe_source)
        if entry is None:
            return {"kind": "unknown", "note": "度量失败（签名漂移？）", "tried": tried[-6:]}
        return _clamp_decision(entry, want, only_if_larger=only_if_larger, widen=widen)

    def write(unreal, obj, decision, target):
        return _u.set_prop_any(obj, ("max_texture_size",), int(decision["after"]))

    probe = "on" if _u.to_bool(probe_source, True) else "off"
    return _drive("opt_texture_clamp_max_size", paths, folder, map, limit, offset,
                  dry_run, confirm, inspect, write,
                  extra={"max_size": want, "only_if_larger": _u.to_bool(
                      only_if_larger, True), "source_probe": probe},
                  list_cap=list_cap)


def _resolve_compression(unreal, name):
    """友好名 -> unreal.TextureCompressionSettings 成员。返回 (值, 来源签名, tried)。"""
    wanted = str(name or "default").strip()
    norm = wanted.lower().replace("_", "").replace("-", "").replace(" ", "")
    aliases = {"default": ("TC_DEFAULT", "TC_Default", "DEFAULT"),
               "normal": ("TC_NORMAL", "TC_Normal", "TC_NORMALMAP", "NORMAL"),
               "masks": ("TC_MASKS", "TC_Masks", "MASKS"),
               "editoricon": ("TC_EDITORICON", "TC_EditorIcon", "EDITORICON"),
               "hdr": ("TC_HDR", "TC_Hdr", "HDR")}
    cands = aliases.get(norm, (wanted, wanted.upper()))
    tried = []
    for enum_name in _COMPRESSION_ENUMS:
        enum = getattr(unreal, enum_name, None)
        if enum is None:
            tried.append({"enum": enum_name, "err": "AttributeMissing"})
            continue
        for member in cands:
            value = getattr(enum, member, None)
            if value is None:
                tried.append({"enum": enum_name, "member": member, "err": "miss"})
                continue
            return value, "%s.%s" % (enum_name, member), tried
    return None, None, tried


def _enum_member(text):
    """从 str(枚举成员) 里取成员名：'<TextureCompressionSettings.TC_DEFAULT: 0>' -> 'TC_DEFAULT'。

    5.4 实测 str() 形态带尖括号与序号；跨版本/本地化文本不同，取不到就返回 None（不误判）。
    """
    if text is None:
        return None
    body = str(text).strip()
    if body.startswith("<") and body.endswith(">"):
        body = body[1:-1]
    if ":" in body:
        body = body.split(":", 1)[0]
    body = body.rsplit(".", 1)[-1].strip()
    return body or None


def _compression_gate(fmt, only_uncompressed, before, target=None):
    """压缩格式写入的"不该动"闸门 -> no_op 决策或 None（继续算收益）。

    两条独立判据，顺序有意义：
      1) before 已经是目标枚举成员 -> no_op（与 only_uncompressed 无关）。5.4 真机 dry-run
         暴露：`only_uncompressed=False` 时把"本来就是 TC_DEFAULT"的贴图也算成待改项，
         既虚报 planned/收益，又会白白保存资产。
      2) only_uncompressed=True 的保守闸门——压缩格式改错会毁观感，默认只碰
         "确定是非工程运行时格式"的：
           * compressed  已是工程格式，不动；
           * high_bpp    TC_HDR 高位深按 8B/px 常驻，改块压缩会丢 HDR 动态范围（美术决策）；
           * unknown     格式文本判不准，宁可漏报也不误改。
     确认要强改由调用方传 only_uncompressed=False。
    """
    cur = _enum_member(before)
    tgt = _enum_member(target)
    if cur and tgt and cur == tgt:
        return {"kind": "no_op", "before": before, "after": before,
                "reason": "已是目标压缩格式 %s，无需改写（不计收益）" % (before,)}
    if not _u.to_bool(only_uncompressed, True):
        return None
    if fmt == "compressed":
        return {"kind": "no_op", "before": before, "after": before,
                "reason": "已是工程运行时压缩格式（only_uncompressed=True 不动）"}
    if fmt == "high_bpp":
        return {"kind": "no_op", "before": before, "after": before,
                "reason": "TC_HDR 高位深按 8B/px 常驻：改块压缩会丢 HDR 动态范围，"
                          "属美术决策；确认要强改传 only_uncompressed=False"}
    if fmt == "unknown":
        return {"kind": "no_op", "before": before, "after": before,
                "reason": "压缩格式文本无法判定(%r)，保守不动；"
                          "确认要改传 only_uncompressed=False" % (before,)}
    return None


@register
def opt_texture_set_compression(paths=None, folder="/Game", map=None, compression="default",
                                only_uncompressed=True, limit=200, offset=0,
                                dry_run=True, confirm=False, probe_source=True, list_cap=80):
    """批量改压缩格式（写·双钥）。默认只动非工程运行时格式（未压缩系 / EditorIcon 系）。

    这是池收益的大头（TODO 里标"可选"但实测最猛）：只改属性，重压缩由编辑器在保存后/
    下次 cook 执行；结果提示需重压缩。两类收益口径不同：
      * uncompressed（真未压缩/浮点系）-> 按 4B/px 计，改 TC_Default 省约 3/4；
      * high_bpp（TC_HDR 高位深）-> 引擎实测按 8B/px 常驻(不降块压缩)，改 TC_Default 约省
        7/8，但会丢 HDR 动态范围，属美术决策 -> 默认 only_uncompressed=True 不动这类；
      * no_stream（TC_EDITOR_ICON / TC_MAX）-> 无 mip 且禁用流送，整张恒常驻池外，
        改回 TC_Default 是"变可流送"，省的是常驻（savings_mb 取 streaming_release_mb）。
    压缩格式写错会毁观感，故默认 only_uncompressed；判不准(unknown)一律不动，
    确认要改再传 only_uncompressed=False。
    """
    def inspect(unreal, obj, target, tried):
        value, hit, sub = _u.get_prop_any(obj, ("compression_settings",))
        before = None if value is None else str(value)
        if before is None:
            return {"kind": "unknown", "note": "读不到 compression_settings",
                    "tried": sub[-6:]}
        fmt = _st.format_class(before)
        target_value, _target_sig, _target_tried = _resolve_compression(unreal, compression)
        gate = _compression_gate(fmt, only_uncompressed, before,
                                 None if target_value is None else str(target_value))
        if gate:
            return gate
        metrics, entry = _entry_of(unreal, obj, target, tried, probe_source)
        if entry is None:
            savings, runtime = 0.0, None
            release = 0.0
        elif fmt == "no_stream":
            savings, runtime = entry["streaming_release_mb"], entry["runtime_mb"]
            release = entry["permanent_mb"]
        else:
            savings, runtime = entry["compression_savings_mb"], entry["runtime_mb"]
            release = 0.0
        return {"kind": "change", "before": before, "after": str(compression),
                "prop_hit": hit, "savings_mb": savings, "format_class": fmt,
                "permanent_release_mb": release or None,
                "runtime_mb": runtime,
                "reason": {"uncompressed": "未压缩系按 4B/px -> %s" % compression,
                           "high_bpp": "高位深(TC_HDR 8B/px) -> %s（会丢 HDR 动态范围）"
                                       % compression,
                           "no_stream": "无 mip/禁用流送(恒常驻) -> %s 可流送" % compression,
                           "compressed": "已压缩格式强制改写 -> %s" % compression}.get(
                           fmt, "格式无法判定 -> %s" % compression)}

    def write(unreal, obj, decision, target):
        value, sig, sub_tried = _resolve_compression(unreal, compression)
        if value is None:
            return False, None, sub_tried
        ok, hit, tried2 = _u.set_prop_any(obj, ("compression_settings",), value)
        return ok, "%s -> %s" % (sig, hit), (sub_tried + tried2)

    probe = "on" if _u.to_bool(probe_source, True) else "off"
    out = _drive("opt_texture_set_compression", paths, folder, map, limit, offset,
                 dry_run, confirm, inspect, write,
                 extra={"compression": str(compression), "only_uncompressed": _u.to_bool(
                     only_uncompressed, True), "source_probe": probe},
                 list_cap=list_cap)
    if isinstance(out, dict) and out.get("ok") and out["result"]["changed"]:
        out["result"]["note"] += " 注意：压缩格式改动需重压缩（编辑器保存后自动/或 Reimport），未 cook 前盘上体积不变。"
    return out
