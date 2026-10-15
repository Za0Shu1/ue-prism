"""纹理流送池模型 + PoolSize 配置读取（纯 stdlib；domain 与 rules 共用的单一事实源）。

分层说明：本模块不 import unreal、不碰总线，只做数学与 ini 解析，故按
pluginpack.ENGINE_MODULES 打包进引擎侧给 domain 用；server/rules 同源取口径，
免得"工具报的池压力"和"perf 报告给的池压力"变成两套估算。

估算模型（保守上界，宁高不低；字段名自带口径）：
- 度量沿用 metrics 的 est_runtime_bytes：BC 近似 1 B/px × 边长² × 面数 × 1.3333（全 mip 链）。
- 尺寸判定用 source_width（源盘反推），不用编辑器冷热态会漂移的直读 width。
- 未压缩系（None/RGBA/Float）按 RGBA8 4 B/px 折算真实占用（real = BC 近似 × 4）；
  编辑器专用系（TC_EDITOR_ICON/TC_MAX）不是 4 B/px，它是"无 mip + 禁用流送"，
  整张恒常驻池外（见 format_class），别把它算成池收益的 3/4。
- 进池判定：
  * 虚拟纹理(VT) -> 走 VT 物理缓存，不计入传统 streaming pool（virtual_mb 单列）；
  * never_stream -> 全链常驻池外（permanent_mb 单列）；
  * 其余（可流送且非虚拟）-> 可流送 mip 视为可全量进池，pool_mb = 全链（上界）。
- 收益三档各自单列（钳尺寸 / 虚拟化 / 改压缩）；合并值按池需求封顶。
  改压缩收益与钳尺寸收益有重叠，故不并入合并值。
- 立方图双假设（#11 D）：Cube 的源存法有两种—立方阵列 6xS^2xB 与
  2:1 全景 2xS^2xB。反推先试阵列（现口径），无 2 的幂解再试全景；两侧按「src 是否
  含因子 3」互斥，容差内也不会互相抢真值。
- 安全阀（#11）：源边长反推没过校验（source_side_exact=false，典型是冷态 Cube 的
  BPP/面数/mip 耦合多解）时只撤"依赖边长的金额报价"，不撤发现与动作——
  clamp/compression 两档恒撤（边长直接进算式），vt/streaming_release 仅在无实测时撤
  （实测 MB 与边长无关）。条目带 size_unverified flag 与 size_basis，
  汇总单列 size_unverified_* 不确定度。
"""
from __future__ import annotations

import os
import re

POOL_MODEL_VERSION = "v4-cube-dual-hypothesis"
POOL_MODEL = {
    "version": POOL_MODEL_VERSION,
    "runtime_basis": "runtime_ledger: 实测 memory_bytes 优先(已含真实格式)，否则 est_runtime_bytes x 格式倍率",
    "size_basis": "source_width (源盘反推，编辑器冷热态稳定)；条目级 size_basis 标注本张实际口径",
    "source_size_basis": "source_memory 反推：候选 BPP{4,8,2,1,16,32,64} x 面积因子（立方图双假设：阵列 6xS^2 / 2:1 全景 2xS^2，其余类用面数），取 2 的幂解",
    "size_verification": "source_side_exact=false -> 撤金额(clamp/compression 恒撤; "
                         "vt/streaming_release 仅无实测时撤)，保留 clamp_to 与 actions; "
                         "条目加 size_unverified flag，汇总加 size_unverified_* 不确定度",
    "clamp_limit_basis": "effective_px = min(源边长, 现有 MaxSize)；已更紧的 MaxSize 不会被放宽",
    "pool_basis": "可流送非虚拟纹理按全链上界计入池；VT 与 never_stream 单列不计入",
    "format_basis": "运行时倍率 BC=1 / 未压缩=4 / 高位深(TC_HDR)=8；无 mip 禁流送系恒常驻池外",
    "savings_basis": "clamp+vt 合并后按池需求封顶；compression 单列不叠加",
}

BC_BYTES_PER_TEXEL = 1.0
UNCOMPRESSED_BYTES_PER_TEXEL = 4.0
UNCOMPRESSED_RATIO = UNCOMPRESSED_BYTES_PER_TEXEL / BC_BYTES_PER_TEXEL
# TC_HDR(RGBM high-resolution range) 不做块压缩：5.4 实测
# /Game/ClientLib/Material/Textures/T_HDR(TextureCube) 常驻 201326592 = 6 x 2048^2 x 8B,
# 与源内存等值。按 BC 的 1B/px 估会把 192MB 的池大户算成 31.9MB(低估 6 倍)。
HDR_BYTES_PER_TEXEL = 8.0

# 源内存反推边长的候选 BPP：源位图是未压缩的，但不一定是 RGBA8。
# 5.4 真机实证 /Game/ClientLib/Material/Textures/T_HDR(TextureCube)：源内存 201326592
# = 6 x 2048^2 x 8B。一律按 4B/px 会反推出 2896 的假尺寸，进而编出"钳到 2048
# 省 96MB"的假收益。首选仍 4(RGBA8)，其次 8(RGBA16/HDR)、16(FP32)、2/1。
SRC_BYTES_PER_TEXEL = 4
SRC_BPP_CANDIDATES = (4, 8, 2, 1, 16, 32, 64)
SRC_SIDE_TOLERANCE = 0.02      # 反推整尺寸回算源内存的允许相对偏差

# 立方图源的两条存法假设（#11 B2 真实工程只读复测定案，16/16 张零例外）：
#  H1 立方阵列源：面积因子 = 面数（6），实测 201,326,592 = 6 x 2048^2 x 8B，÷6 余 0
#  H2 2:1 全景源（多为 RGBE 4B/px）：面积因子 = 2，实测 134,217,728 = 2 x 4096^2 x 4B
# 互斥性：BPP 候选与边长解都是 2 的幂，H1 解含因子 3、H2 解不含；容差内也抢不到
# 同一条 src（2^m/3 恒偏离 1 达 33%），所以先 H1 后 H2 不会把既往正确的金额推错。
CUBE_FACES = 6
PANORAMA_AREA_FACTOR = 2
LAYOUT_FACE_STACK = "face_stack"
LAYOUT_CUBE_ARRAY = "cube_array"
LAYOUT_PANORAMA_2TO1 = "panorama_2to1"

# 5.4 实证：Cube/Volume 不支持流式虚拟纹理（编辑器勾选框置灰）；2D Array 可行。
VT_UNSUPPORTED_HINTS = ("cubetexture", "texturecube", "volumetexture", "texturevolume")

# 两类"非流送友好"格式，物理后果不同，必须分开算（别把收益虚报成同一个数）：
#  - no_stream：TC_EDITOR_ICON / TC_MAX 这类——编辑器里按"无 mip + 禁用流送"处理，
#    整张贴恒常驻（池外但死占显存），所以 VT/钳尺寸的收益路径对它不成立；
#  - uncompressed：真正未压缩/浮点系（None/RGBA/Float）——按 4B/px 计，比 BC 贵 4 倍。
NO_STREAM_FORMATS = ("editoricon", "tcmax")
UNCOMPRESSED_FORMATS = ("tcnone", "none", "uncompressed", "argb", "rgba", "bgra",
                        "floatingpoint", "float")
# 明确是压缩格式的写法先排除，避免 default/normal 被 "none" 这类子串误伤
# 名字带 HDR 但实际按高位深常驻的一类，单独一档（format_class 里先于 compressed 判）
HIGH_BPP_FORMATS = ("tchdr",)
# 明确是压缩格式的写法先排除，避免 default/normal 被 "none" 这类子串误伤
COMPRESSED_HINTS = ("tcdefault", "tcnormal", "tcmasks", "bc1", "bc3",
                    "bc4", "bc5", "bc7", "default", "normal", "masks")

_INI_SECTION = re.compile(r"^\s*\[(?P<sec>[^\]]+)\]\s*$")
_INI_POOL = re.compile(
    r"^\s*[-+]?\s*(?P<key>[A-Za-z0-9_.]*PoolSize[A-Za-z0-9_.]*)\s*=\s*(?P<val>-?\d+(?:\.\d+)?)\s*$",
    re.IGNORECASE)
_INI_FLAG = re.compile(
    r"^\s*[-+]?\s*(?P<key>[A-Za-z0-9_.]*(?:VirtualTextures?|VirtualTextureSupport)[A-Za-z0-9_.]*)\s*=\s*(?P<val>[^\s;]+)\s*$",
    re.IGNORECASE)


def _as_float(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return float(default)


def _mb(nbytes):
    return round(float(nbytes) / 1048576.0, 3)


def to_px(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def next_pow2_floor(n):
    """<= n 的最大 2 的幂（钳尺寸建议值）。n<1 -> 0。"""
    n = to_px(n)
    if n < 1:
        return 0
    p = 1
    while p * 2 <= n:
        p *= 2
    return p


def clamp_suggestion(source_px, max_px):
    """尺寸超阈值时建议钳到多少；已在阈值内/无阈值 -> None（不建议动）。"""
    src = to_px(source_px)
    lim = to_px(max_px)
    if src <= 0 or lim <= 0 or src <= lim:
        return None
    target = next_pow2_floor(lim)
    if target <= 0 or target >= src:
        return None
    return target


def norm_class(cls_name):
    return str(cls_name or "").replace(" ", "").lower()


def is_pow2(n):
    n = to_px(n)
    return n > 0 and (n & (n - 1)) == 0


def is_cube_class(cls_name):
    """立方图判定（与 faces_for 同源）：只有立方图需要双假设，
    Texture2DArray 的 6 个 slice 不算—面数相同但源存法不是立方。"""
    return "cube" in norm_class(cls_name)


def faces_for(cls_name, array_size=None):
    """一张纹理的"面数"：Cube 6 面，Texture2DArray 再乘 slice 数。"""
    faces = CUBE_FACES if is_cube_class(cls_name) else 1
    try:
        n = int(array_size or 1)
    except (TypeError, ValueError):
        n = 1
    return faces * max(n, 1)


def source_layout_from_memory(src_mem_bytes, faces=1, bpp_candidates=None, cube=False):
    """由 source_memory_bytes 反推源边长（源位图不带 mip）；立方图可走双假设。

    候选面积因子：面数（H1 立方阵列 / 普通类），立方图再追加 2（H2 2:1 全景）。
    取「结果恰是 2 的幂」的那档 BPP；全不中才回落到首选 4B/px 的四舍五入
    值并用 exact=False 标注是估计。
    side 恒是**面边长**、faces 恒是**运行时面数**：全景命中时源面积只有
    2xS^2，运行时仍按 6 面算——这正是把 hDRI 类低估 3 倍的账纠回来的地方。
    无解 -> side=0 / exact=False / layout=None。
    fallback（无 2 的幂解）仍取第一条假设：那些数字只用来标注发现，金额恒被阀撤回，
    不用一条更想当的假设去伪装精确。
    """
    faces_n = max(to_px(faces), 1)
    empty = {"side": 0, "bytes_per_texel": 0, "faces": faces_n,
             "exact": False, "layout": None, "area_factor": 0.0}
    try:
        total = float(src_mem_bytes)
    except (TypeError, ValueError):
        return empty
    if total <= 0:
        return empty
    cands = tuple(bpp_candidates) if bpp_candidates else SRC_BPP_CANDIDATES
    hypotheses = [(float(faces_n), LAYOUT_CUBE_ARRAY if cube else LAYOUT_FACE_STACK)]
    if cube:
        hypotheses.append((float(PANORAMA_AREA_FACTOR), LAYOUT_PANORAMA_2TO1))
    fallback = None
    for factor, layout in hypotheses:
        rows = []
        for bpp in cands:
            area = total / (float(bpp) * factor)
            if area <= 0:
                continue
            side = int(round(area ** 0.5))
            if side <= 0:
                continue
            err = abs((side * side * float(bpp) * factor) - total) / total
            if is_pow2(side) and err <= SRC_SIDE_TOLERANCE:
                return {"side": side, "bytes_per_texel": int(bpp),
                        "faces": faces_n, "exact": True,
                        "layout": layout, "area_factor": factor}
            rows.append((side, int(bpp)))
        if rows and fallback is None:
            fallback = (rows[0][0], rows[0][1], layout, factor)
    if fallback:
        return {"side": fallback[0], "bytes_per_texel": fallback[1],
                "faces": faces_n, "exact": False,
                "layout": fallback[2], "area_factor": fallback[3]}
    return empty


def source_side_from_memory(src_mem_bytes, faces=1, bpp_candidates=None, cube=False):
    """历史 4 元组口径 (side, bytes_per_texel, faces, exact)。

    数学与布局判定只有 source_layout_from_memory 一份，这里只做投影，
    避免两套口径漂移；要区分命中了哪条假设直接用 layout 字典版。
    """
    got = source_layout_from_memory(src_mem_bytes, faces, bpp_candidates, cube)
    return got["side"], got["bytes_per_texel"], got["faces"], got["exact"]


def effective_px(source_px, max_size_px):
    """运行时真正受限的边长：源尺寸与 MaxSize 取小（MaxSize<=0 视为没限）。"""
    src = to_px(source_px)
    cap = to_px(max_size_px)
    if src <= 0:
        return cap
    if cap <= 0:
        return src
    return min(src, cap)


def resize_delta_mb(real_mb, current_px, target_px):
    """把运行时受限边长从 current_px 改到 target_px 的显存/池变化量（MB）。

    正=省，负=变大（放宽/取消 MaxSize 是负数，不能报成收益）。按面积平方比折算，
    与 texture_pool_entry 的 clamp_savings_mb 同一个式子——两边不会打架。
    """
    cur = to_px(current_px)
    want = to_px(target_px)
    if cur <= 0 or want <= 0 or cur == want:
        return 0.0
    ratio = (float(want) / float(cur)) ** 2
    return round(_as_float(real_mb) * (1.0 - ratio), 3)


def vt_support(cls_name):
    """(supported, reason)：Cube/Volume 明确不支持 VT；类名缺失时保守判可行。"""
    key = norm_class(cls_name)
    for hint in VT_UNSUPPORTED_HINTS:
        if hint in key:
            return False, "class %s 不支持流式虚拟纹理（5.4 实证勾选框置灰）" % cls_name
    if not key:
        return True, "class 未知，按可行处理（以编辑器为准）"
    return True, ""


def _norm_format(text):
    """枚举文本归一：去空格/下划线/连字符并小写（TC_EDITOR_ICON 与 TC_EDITORICON 同判）。"""
    s = str(text or "")
    for ch in (" ", "_", "-", "\t"):
        s = s.replace(ch, "")
    return s.lower()


def format_class(compression):
    """compression_settings 文本 -> "compressed" | "uncompressed" | "no_stream" | "high_bpp" | "unknown"。

    只认枚举成员名（Python 侧 str(enum) 给的是名字，不是本地化显示名）；本地化/未知文本
    一律 "unknown"，宁可漏报也不误改美术贴图的压缩格式。
    """
    s = _norm_format(compression)
    if not s:
        return "unknown"
    for hint in HIGH_BPP_FORMATS:
        if hint in s:
            return "high_bpp"
    for good in COMPRESSED_HINTS:
        if good in s:
            return "compressed"
    for hint in NO_STREAM_FORMATS:
        if hint in s:
            return "no_stream"
    for hint in UNCOMPRESSED_FORMATS:
        if hint in s:
            return "uncompressed"
    return "unknown"


def is_uncompressed(compression):
    """兼容谓词：仅"真未压缩系"为 True（无 mip/禁流送系走 format_class=='no_stream'）。"""
    return format_class(compression) == "uncompressed"


def is_high_bpp(compression):
    """高位深不降格式（TC_HDR）：名义上是"压缩设置"，运行时其实按 8B/px 常驻。"""
    return format_class(compression) == "high_bpp"


def runtime_ratio(compression):
    """压缩格式 -> 相对 BC(1B/px) 的运行时倍率：未压缩 4 / 高位深 8 / 其余与判不准 1。"""
    if is_high_bpp(compression):
        return HDR_BYTES_PER_TEXEL / BC_BYTES_PER_TEXEL
    if is_uncompressed(compression):
        return UNCOMPRESSED_RATIO
    return 1.0


def px_of(metrics):
    """判定用边长：优先 source_width（源盘反推），退回直读 width。"""
    m = metrics or {}
    src = to_px(m.get("source_width"))
    if src > 0:
        return src
    return to_px(m.get("width"))


def size_basis(metrics):
    """本张的尺寸口径（条目级；与 POOL_MODEL 的口径说明呼应，供消费方判金额可不可信）。

    - source_exact: 边长来自 source_memory 反推且通过校验
    - source_unverified: 反推无可信解（典型：真实工程 4096 立方图被推成 2365）
      -> 依赖边长的金额由安全阀撤回，读数只当线索
    - runtime_px: 没有源尺寸（probe_source=False 或读不到源内存），只有直读 width
    - unknown: 两者都没有
    """
    m = metrics or {}
    if m.get("source_side_exact") is False:
        return "source_unverified"
    if to_px(m.get("source_width")) > 0:
        return "source_exact"
    if to_px(m.get("width")) > 0:
        return "runtime_px"
    return "unknown"


def runtime_ledger(metrics):
    """运行时字节台账（domain 的真大/假大判定与池估算同源，防两套账打架）。

    引擎实测(blueprint_get_memory_size)本身已含真实格式，凡是不低于 BC 下限就采信，
    **不再乘格式倍率**——乘了会把 192MB 报成 1.5GB。实测还没上传（典型占位 4096B，
    远低于 BC 下限）时，才用"BC 近似 x 格式倍率"兜底：TC_HDR 不折就是 6 倍低估。
    保守上界口径：判不出格式按 BC 算，宁缺毋滥。
    """
    m = metrics or {}
    measured = _as_float(m.get("memory_bytes"))
    est_bc = _as_float(m.get("est_runtime_bytes"))
    ratio = runtime_ratio(m.get("compression"))
    derived = est_bc * ratio
    if measured > 0 and measured >= est_bc:
        return {"runtime_bytes": measured,
                "bc_equivalent_bytes": measured / ratio,
                "runtime_ratio": ratio,
                "runtime_bytes_per_texel": BC_BYTES_PER_TEXEL * ratio,
                "runtime_basis": "resident_memory"}
    return {"runtime_bytes": max(measured, derived),
            "bc_equivalent_bytes": est_bc,
            "runtime_ratio": ratio,
            "runtime_bytes_per_texel": BC_BYTES_PER_TEXEL * ratio,
            "runtime_basis": "derived_estimate"}


def texture_pool_entry(metrics, cls_name=None, disk_bytes=None, px_error=4096, px_warn=2048,
                     min_vt_mb=4.0, min_clamp_mb=1.0):
    """一张纹理的池占用 + 可优化项（纯函数，可离线测；键位常驻，值可为 0/None）。

    返回：runtime_mb / bc_equivalent_mb / uncompressed_mb / pool_mb / permanent_mb /
    virtual_mb / clamp_to / clamp_savings_mb / vt_savings_mb / compression_savings_mb /
    vt_supported / vt_reason / actions[] / flags[] / runtime_basis / runtime_bytes_per_texel
    / source_side_exact / size_basis

    安全阀：size_basis=source_unverified 时金额字段可能是 None（不是 0.0——0.0 的含义是
    "确认没有收益"），但 clamp_to 与 actions 保留：发现不能因为算不准就隐身（#7 踩过）。
    """
    m = metrics or {}
    fmt = format_class(m.get("compression"))
    uncompressed = fmt == "uncompressed"
    no_stream = fmt == "no_stream"
    high_bpp = fmt == "high_bpp"
    ledger = runtime_ledger(m)
    real = ledger["runtime_bytes"]
    bc = ledger["bc_equivalent_bytes"]
    src_px = px_of(m)
    eff_px = to_px(m.get("effective_width") or m.get("width"))
    max_size = to_px(m.get("max_size"))
    virtual = bool(m.get("virtual_texture"))
    never = bool(m.get("never_stream"))
    supported, reason = vt_support(cls_name)
    # 安全阀开关：只有域层"显式标了不置信"才撤金额。缺键(None)不挡——
    # probe_source=False 的保守路径与热态实测路径都不得被误伤。
    unverified = m.get("source_side_exact") is False
    measured = ledger["runtime_basis"] == "resident_memory"

    entry = {
        "class": cls_name,
        "disk_mb": _mb(disk_bytes) if disk_bytes else None,
        "source_width": src_px or None,
        "effective_width": eff_px or None,
        "max_size": max_size or None,
        "compression": m.get("compression"),
        "virtual_texture": virtual,
        "never_stream": never,
        "format_class": fmt,
        "uncompressed": uncompressed,
        "high_bpp": high_bpp,
        "non_streaming_format": no_stream,
        "runtime_ratio": ledger["runtime_ratio"],
        "runtime_basis": ledger["runtime_basis"],
        "runtime_bytes_per_texel": ledger["runtime_bytes_per_texel"],
        "source_side_exact": m.get("source_side_exact"),
        "size_basis": size_basis(m),
        "streaming_release_mb": 0.0,
        "vt_supported": supported,
        "vt_reason": reason or None,
        "runtime_mb": _mb(real),
        "bc_equivalent_mb": _mb(bc),
        "uncompressed_mb": _mb(real) if uncompressed else 0.0,
        "pool_mb": 0.0,
        "permanent_mb": 0.0,
        "virtual_mb": 0.0,
        "clamp_to": None,
        "clamp_savings_mb": 0.0,
        "vt_savings_mb": 0.0,
        "compression_savings_mb": 0.0,
        "actions": [],
        "flags": [],
    }
    if unverified:
        entry["flags"].append("size_unverified")
    if real <= 0:
        entry["flags"].append("no_runtime_metrics")
        return entry

    if virtual:
        entry["virtual_mb"] = _mb(real)
        entry["flags"].append("already_virtual")
    elif never or no_stream:
        # 禁流送（无 mip / never_stream）恒常驻：不进池，但死占显存，是另一本账
        entry["permanent_mb"] = _mb(real)
        entry["flags"].append("never_stream_permanent" if never
                              else "non_streaming_format_permanent")
    else:
        entry["pool_mb"] = _mb(real)

    if uncompressed:
        entry["flags"].append("uncompressed_format")
        # bc 按边长算 -> 边长不可信则省额不可信；格式是直读的，发现照报、动作照给
        entry["compression_savings_mb"] = None if unverified else _mb(real - bc)
        entry["actions"].append("set_compression")
    elif high_bpp:
        # TC_HDR 的 8B/px 是真金白银的池占用，但改成 BC 会丢动态范围 —— 只报账不默认改写，
        # 不进 actions（opt_texture_set_compression 需显式 only_uncompressed=False 才动）。
        entry["flags"].append("high_bpp_format")
        entry["compression_savings_mb"] = None if unverified else _mb(real - bc)
    elif no_stream:
        entry["flags"].append("non_streaming_format")
        # 改回 TC_Default 的收益是"从恒常驻变可流送"，量级=全链，但不等于池收益
        entry["streaming_release_mb"] = (None if (unverified and not measured)
                                         else entry["permanent_mb"])
        entry["actions"].append("set_compression")

    if not virtual:
        # 建议只在收益够大时给：小纹理开 VT / 钳尺寸是噪音（阈值由 profile 传入）
        if supported and entry["pool_mb"] >= _as_float(min_vt_mb):
            # VT 移出的是"当前进池量"：有实测就照报；纯反推时它随边长一起不可信
            entry["vt_savings_mb"] = (None if (unverified and not measured)
                                      else entry["pool_mb"])
            entry["actions"].append("enable_vt")
        elif not supported:
            entry["flags"].append("vt_unsupported")
        judged = src_px or eff_px
        limit = effective_px(judged, max_size)   # 现有 MaxSize 已生效时按它算，别重复计收益
        if limit > px_warn:
            sug = clamp_suggestion(limit, px_warn)
            if sug:
                saving = resize_delta_mb(_mb(real), limit, sug)
                entry["clamp_to"] = sug
                if unverified:
                    # 省额按 (limit -> sug) 面积比算，limit 本身就不可信 -> 金额撤；
                    # 没法用 min_clamp_mb 比较，但超限这件事仍值得报，动作保留
                    entry["clamp_savings_mb"] = None
                    entry["actions"].append("clamp_max_size")
                else:
                    entry["clamp_savings_mb"] = saving
                    if saving >= _as_float(min_clamp_mb):
                        entry["actions"].append("clamp_max_size")
                if judged >= px_error:
                    entry["flags"].append("oversized_mip_chain")
    if max_size > 0 and src_px and max_size < src_px:
        entry["flags"].append("max_size_capped")   # 运行时已被 MaxSize 限住（假大常见根因）
    if entry["pool_mb"] <= 0.0 and entry["runtime_mb"] > 0.0:
        entry["flags"].append("not_in_pool")       # VT 或 never_stream 导致的池外
    return entry


def aggregate(entries):
    """汇总池压力 + 三档收益（分列；合并值按池需求封顶，防同一 MB 算三遍）。

    安全阀撤回的 None 金额按 0 计入合计（不猜数），规模不确定度单列
    size_unverified_count / size_unverified_pool_mb / size_unverified_permanent_mb，
    被撤回过报价的条目数记 savings_withheld_count。
    """
    out = {"textures": 0, "measured": 0, "unmeasured": 0,
           "runtime_mb": 0.0, "pool_demand_mb": 0.0, "permanent_mb": 0.0,
           "virtualized_mb": 0.0,
           "clamp_savings_mb": 0.0, "vt_savings_mb": 0.0, "compression_savings_mb": 0.0,
           "potential_pool_release_mb": 0.0,
           "streaming_release_mb": 0.0, "potential_permanent_release_mb": 0.0,
           "need_clamp_count": 0, "need_vt_count": 0, "uncompressed_count": 0,
           "non_streaming_count": 0, "never_stream_count": 0, "vt_blocked_count": 0,
           "oversized_count": 0, "high_bpp_count": 0,
           "size_unverified_count": 0, "size_unverified_pool_mb": 0.0,
           "size_unverified_permanent_mb": 0.0, "savings_withheld_count": 0}
    for e in entries or []:
        out["textures"] += 1
        if not _as_float(e.get("runtime_mb")):
            out["unmeasured"] += 1
            continue
        out["measured"] += 1
        out["runtime_mb"] += _as_float(e.get("runtime_mb"))
        out["pool_demand_mb"] += _as_float(e.get("pool_mb"))
        out["permanent_mb"] += _as_float(e.get("permanent_mb"))
        out["virtualized_mb"] += _as_float(e.get("virtual_mb"))
        out["clamp_savings_mb"] += _as_float(e.get("clamp_savings_mb"))
        out["vt_savings_mb"] += _as_float(e.get("vt_savings_mb"))
        out["compression_savings_mb"] += _as_float(e.get("compression_savings_mb"))
        acts = e.get("actions") or []
        if "clamp_max_size" in acts:
            out["need_clamp_count"] += 1
        if "enable_vt" in acts:
            out["need_vt_count"] += 1
        out["streaming_release_mb"] += _as_float(e.get("streaming_release_mb"))
        if e.get("uncompressed"):
            out["uncompressed_count"] += 1
        if e.get("high_bpp"):
            out["high_bpp_count"] += 1
        if e.get("non_streaming_format"):
            out["non_streaming_count"] += 1
        if e.get("never_stream"):
            out["never_stream_count"] += 1
        if e.get("vt_supported") is False:
            out["vt_blocked_count"] += 1
        if "oversized_mip_chain" in (e.get("flags") or []):
            out["oversized_count"] += 1
        if "size_unverified" in (e.get("flags") or []):
            out["size_unverified_count"] += 1
            out["size_unverified_pool_mb"] += _as_float(e.get("pool_mb"))
            out["size_unverified_permanent_mb"] += _as_float(e.get("permanent_mb"))
        for _money_key in ("clamp_savings_mb", "vt_savings_mb",
                           "compression_savings_mb", "streaming_release_mb"):
            if e.get(_money_key) is None:
                out["savings_withheld_count"] += 1
                break
    for key in list(out.keys()):
        if key.endswith("_mb"):
            out[key] = round(out[key], 3)
    out["potential_pool_release_mb"] = round(
        min(out["pool_demand_mb"], out["vt_savings_mb"] + out["clamp_savings_mb"]), 3)
    out["potential_permanent_release_mb"] = round(
        min(out["permanent_mb"], out["streaming_release_mb"]), 3)
    out["model"] = POOL_MODEL
    return out


def compare_pool(demand_mb, pool_mb, tight_pct=80.0, min_demand_mb=32.0):
    """纯逻辑：池需求(上界) vs 工程配置池 -> verdict。

    - unknown   配置里读不到 PoolSize（不猜引擎默认值）
    - disabled  池配成 <=0（UE 语义=不设上限/全量常驻，需求无处收缩）
    - overflow  需求 > 池（必然反复淘汰/爆池）
    - tight     需求 >= 池 * tight_pct% 且已过关注线
    - ok        其余
    """
    demand = _as_float(demand_mb)
    out = {"demand_mb": round(demand, 3), "pool_mb": None, "overflow_mb": 0.0,
           "headroom_mb": None, "ratio_pct": None, "verdict": "unknown", "note": ""}
    if pool_mb is None:
        out["note"] = ("未从工程 ini 读到 *PoolSize（可能走引擎默认或代码期设置）："
                       "只报需求，不报超限。")
        return out
    pool = _as_float(pool_mb)
    out["pool_mb"] = round(pool, 3)
    if pool <= 0:
        out["verdict"] = "disabled"
        out["note"] = "PoolSize<=0：UE 语义为不设流送池上限（等价全部常驻），只能降需求。"
        return out
    out["ratio_pct"] = round(100.0 * demand / pool, 2)
    out["headroom_mb"] = round(pool - demand, 3)
    if demand > pool:
        out["verdict"] = "overflow"
        out["overflow_mb"] = round(demand - pool, 3)
    elif demand >= pool * (tight_pct / 100.0) and demand >= _as_float(min_demand_mb):
        out["verdict"] = "tight"
    else:
        out["verdict"] = "ok"
    return out


def _kind_of(key, section, rel_path):
    k = str(key).lower()
    sec = str(section or "").lower()
    dirs = [x for x in str(rel_path or "").replace("\\", "/").lower().split("/")[:-1] if x]
    platform = bool([x for x in dirs if x != "config"]) or "targetsettings" in sec \
        or "targetplatformoptions" in sec or "deviceprofile" in sec
    if platform:
        return "platform"
    if k.startswith("sg."):
        return "scalability"
    if k.startswith("r."):
        return "cvar"
    if sec.startswith("/script/engine.streamingsettings"):
        return "streamingsettings"
    return "other"


# 取值优先级：平台/设备 profile > 引擎 cvar > StreamingSettings > scalability > 其他
_KIND_RANK = {"platform": 0, "cvar": 1, "streamingsettings": 2, "scalability": 3, "other": 4}


def read_pool_config(project_dir):
    """解析 <project>/Config/**/*.ini 找流送池与虚拟化开关（只读磁盘，不需引擎）。

    返回 {"pool_candidates":[{key,value_mb,section,file,line,kind}], "effective_mb",
    "effective", "flags":{key:val}, "files_scanned", "config_roots", "note"}。
    候选全列（改池常散落 DefaultEngine/DefaultScalability/平台 ini）；effective 按
    _KIND_RANK 取最具体的一条，同类并列取最大值（大工程通常按最大池调）。
    """
    out = {"pool_candidates": [], "effective_mb": None, "effective": None,
           "flags": {}, "files_scanned": 0, "config_roots": [], "note": ""}
    proj = str(project_dir or "")
    cfg_root = os.path.join(proj, "Config")
    if not os.path.isdir(cfg_root):
        out["note"] = "无 Config 目录（project_dir 未解析或不是工程根）"
        return out
    for dirpath, dirnames, filenames in os.walk(cfg_root):
        dirnames.sort()
        for fn in sorted(filenames):
            if not fn.lower().endswith(".ini"):
                continue
            path = os.path.join(dirpath, fn)
            rel = os.path.relpath(path, proj).replace("\\", "/")
            out["files_scanned"] += 1
            section = ""
            try:
                with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
                    lines = fh.readlines()
            except OSError:
                continue
            for i, raw in enumerate(lines):
                line = raw.split(";", 1)[0].split("//", 1)[0].rstrip()
                sec = _INI_SECTION.match(line)
                if sec:
                    section = sec.group("sec")
                    rel_dir = os.path.dirname(rel)
                    if rel_dir and rel_dir not in out["config_roots"]:
                        out["config_roots"].append(rel_dir)
                    continue
                hit = _INI_POOL.match(line)
                if hit:
                    out["pool_candidates"].append({
                        "key": hit.group("key"), "value_mb": _as_float(hit.group("val")),
                        "section": section, "file": rel, "line": i + 1,
                        "kind": _kind_of(hit.group("key"), section, rel)})
                    continue
                fl = _INI_FLAG.match(line)
                if fl:
                    out["flags"][str(fl.group("key")).lower()] = str(fl.group("val")).strip().lower()
    cands = out["pool_candidates"]
    if cands:
        cands.sort(key=lambda c: (_KIND_RANK.get(c["kind"], 9), -c["value_mb"],
                                  c["file"], c["line"]))
        best = cands[0]
        out["effective_mb"] = best["value_mb"]
        out["effective"] = dict(best)
        out["note"] = "%d 处 PoolSize 候选，取最具体者（%s @ %s#L%d）" % (
            len(cands), best["key"], best["file"], best["line"])
    else:
        out["note"] = ("Config ini 未发现任何 *PoolSize（走引擎默认；大工程常见根因是没配池，"
                       "而不是池配小了）")
    return out


def vt_enabled(config):
    """从 read_pool_config 的 flags 判工程是否启用 VT（查不到返回 None=未知，不猜）。"""
    flags = (config or {}).get("flags") or {}
    for key in ("r.virtualtextures", "benablevirtualtexturesupport"):
        if key in flags:
            return flags[key] in ("1", "true", "yes", "on")
    for key in flags:
        if "virtualtexture" in key:
            return flags[key] in ("1", "true", "yes", "on")
    return None
