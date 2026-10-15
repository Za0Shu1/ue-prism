# -*- coding: utf-8 -*-
"""#11 C：uasset 内嵌字节里的源图头扫描（纯 stdlib，探针与 L0 共用同一判据）。

背景：B1 实测 UE 存进包内的"源"是**裸位图**（float 类 source_disk==source_memory，
PNG/DDS 头不在尺寸度量里）。这条把"读包内存到的源"推到什么程度，必须拿字节面实证，
不能靠猜——本模块给探针(probe_source_artifacts.py)三个可离线验证的问题：

1) 包里到底有没有源文件的**容器头**（PNG/JPEG/BMP/DDS 签名）？签名之后按各格式
   头结构做**二次校验**（自洽的声明尺寸/头部长度），随机噪声不许误报；
2) 名字表里有没有"源数据"属性的痕迹（SourceFileData/SourceFileType/...）——
   有痕迹说明该版本序列化器存过源容器，值得进一步解析；
3) 若有头：头里的宽高与 authored 真值对不对得上（真值由 make_dds 靶自带）。

签名与校验全部保守：宁可漏报也不虚报。无命中如实返回空列表。
"""

PNG_SIG = b"\x89PNG\r\n\x1a\n"
DDS_SIG = b"DDS "
BMP_SIG = b"BM"
JPEG_SIG = b"\xff\xd8\xff"
MIN_SIDE, MAX_SIDE = 8, 32768

NAME_TOKENS = ("SourceFileData", "SourceFileType", "AssetImportData",
               "InterchangeSourceData", "TextureSourceData",
               "bStoreSourceDataAsBMP", "EditorImportedObjectPath")


def _u32(data, off):
    return int.from_bytes(data[off:off + 4], "little")


def _s32(data, off):
    return int.from_bytes(data[off:off + 4], "little", signed=True)


def _be16(data, off):
    return int.from_bytes(data[off:off + 2], "big")


def _be32(data, off):
    return int.from_bytes(data[off:off + 4], "big")


def _side_ok(v):
    return MIN_SIDE <= v <= MAX_SIDE


def parse_png(data, off):
    if off + 29 > len(data) or data[off + 12:off + 16] != b"IHDR":
        return None
    if _be32(data, off + 8) != 13:
        return None
    w, h = _be32(data, off + 16), _be32(data, off + 20)
    if not (_side_ok(w) and _side_ok(h)):
        return None
    return {"kind": "png", "offset": off, "w": w, "h": h,
            "bitdepth": data[off + 24], "colortype": data[off + 25]}


def parse_jpeg(data, off):
    p = off + 2
    n = len(data)
    while p + 4 <= n:
        if data[p] != 0xFF:
            p += 1
            continue
        while p < n and data[p] == 0xFF:
            p += 1
        if p >= n:
            return None
        marker = data[p]
        p += 1
        if marker in (0x01, 0xD8) or 0xD0 <= marker <= 0xD7:
            continue
        if p + 2 > n:
            return None
        seglen = _be16(data, p)
        if seglen < 2:
            return None
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                      0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            if p + 7 > n:
                return None
            h, w = _be16(data, p + 3), _be16(data, p + 5)
            if _side_ok(w) and _side_ok(h):
                return {"kind": "jpeg", "offset": off, "w": w, "h": h,
                        "bpc": data[p + 2]}
            return None
        p += seglen
    return None


def parse_dds(data, off):
    if off + 148 > len(data) or _u32(data, off + 4) != 124:
        return None
    if _u32(data, off + 76) != 32:
        return None
    h, w = _u32(data, off + 12), _u32(data, off + 16)
    if not (_side_ok(w) and _side_ok(h)):
        return None
    info = {"kind": "dds", "offset": off, "w": w, "h": h}
    if data[off + 84:off + 88] == b"DX10":
        info["dxgi_format"] = _u32(data, off + 128)
        info["resource_dimension"] = _u32(data, off + 132)
        info["misc_flag"] = _u32(data, off + 136)
        info["array_size"] = _u32(data, off + 140)
    return info


def parse_bmp(data, off):
    if off + 30 > len(data):
        return None
    file_size = _u32(data, off + 2)
    pix_off = _u32(data, off + 10)
    hdr_size = _u32(data, off + 14)
    if hdr_size not in (12, 40, 56, 108, 124):
        return None
    if not (14 + hdr_size <= file_size <= len(data) - off):
        return None
    if pix_off < 14 + hdr_size or pix_off > file_size:
        return None
    if hdr_size == 12:
        w = int.from_bytes(data[off + 18:off + 20], "little")
        h = int.from_bytes(data[off + 20:off + 22], "little")
        bpp = int.from_bytes(data[off + 24:off + 26], "little")
    else:
        w, h = _s32(data, off + 18), _s32(data, off + 22)
        bpp = int.from_bytes(data[off + 28:off + 30], "little")
    if not (_side_ok(abs(w)) and _side_ok(abs(h)) and bpp in (1, 2, 4, 8, 16, 24, 32)):
        return None
    expect = pix_off + ((abs(w) * bpp + 7) // 8) * abs(h)
    if expect > file_size + 16 * 1024 * 1024:
        return None
    return {"kind": "bmp", "offset": off, "w": abs(w), "h": abs(h),
            "bpp": bpp, "file_size": file_size}


_PARSERS = ((PNG_SIG, parse_png), (JPEG_SIG, parse_jpeg),
            (DDS_SIG, parse_dds), (BMP_SIG, parse_bmp))


def scan_blob(data, cap=50):
    """按签名找全部命中；每类最多 cap 条，offset 升序。"""
    hits = []
    for sig, parser in _PARSERS:
        found = []
        i = data.find(sig)
        while i != -1:
            if len(found) >= cap:
                break
            info = parser(data, i)
            if info is not None:
                found.append(info)
            i = data.find(sig, i + 1)
        hits.extend(found)
    hits.sort(key=lambda x: x["offset"])
    return hits


def scan_names(data):
    """名字表痕迹：token -> 出现次数（uasset 的名字表是明文字符串）。"""
    out = {}
    for token in NAME_TOKENS:
        needle = token.encode("ascii")
        n, i = 0, data.find(needle)
        while i != -1:
            n += 1
            i = data.find(needle, i + 1)
        out[token] = n
    return out