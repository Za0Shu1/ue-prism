# -*- coding: utf-8 -*-
"""已知真值 DDS 源图生成（纯 stdlib，无 PIL/numpy，可在系统 Python 与 UE 内嵌 Python 跑）。

为什么用 DDS（#11 B）：冷态源边长是按 source_memory 反推的，歧义只发生在
"每像素字节数(BPT) 不在候选先验的前两档(4/8)"的源上——浮点(16/32B)、单通道(1/2B)。
PNG 只能给出 8/16-bit 定点且通道数由导入器决定，真值不干净；DDS 的 DX10 头能把
**边长 / BPT / 面数** 三件事一次钉死，于是"已知真值"由本文件写出，不依赖引擎实现。

产出 <out_dir>/*.dds + truth.json：
  truth.json 每项给 authored 尺寸/面数/BPT 与 payload 字节数，夹具校验据此比对
  引擎侧读到的 source_memory_bytes 与反推出的 source_width/size_basis。

注意：源图刻意只写 1 级 mip（FTextureSource 语义"源位图不带 mip"），
让 source_memory_bytes 与 w*h*bpt*faces 可直接对齐，跨版本歧义面最小。
"""
import json
import os
import struct

MAGIC = b"DDS "

# DDS_HEADER.dwFlags
DDSD_CAPS = 0x1
DDSD_HEIGHT = 0x2
DDSD_WIDTH = 0x4
DDSD_PITCH = 0x8
DDSD_PIXELFORMAT = 0x1000
DDSD_MIPMAPCOUNT = 0x20000
DDSD_LINEARSIZE = 0x80000
DDSD_DEPTH = 0x800000

# DDS_PIXELFORMAT.dwFlags
DDPF_FOURCC = 0x4
DDPF_RGB = 0x40
DDPF_ALPHA = 0x2
DDPF_LUMINANCE = 0x200

# DDS_HEADER.dwCaps / dwCaps2
DDSCAPS_TEXTURE = 0x1000
DDSCAPS_COMPLEX = 0x8
DDSCAPS_MIPMAP = 0x400000
DDSCAPS2_CUBEMAP = 0x200
DDSCAPS2_CUBEMAP_ALL_FACES = 0xFC00

# DDS_HEADER_DXT10。5.4 实证：立方图不是 resourceDimension=4(那是 TEXTURE3D，
# 引擎回 "3D textures must have array size of 1" -> Error=5 拒读)；立方图 =
# resourceDimension 仍是 TEXTURE2D(3) + miscFlag=DDS_RESOURCE_MISC_TEXTURECUBE(0x2)
# + arraySize=6。
DX10_RESOURCE_DIMENSION_TEXTURE2D = 3
DX10_MISC_TEXTURECUBE = 0x2
DX10_MISC_FLAGS2_ALPHA_UNKNOWN = 0x0

# DXGI_FORMAT（只用到这几档）。校验比的是字节数：同宽子类型
# (UNORM/SNORM/UINT/FLOAT) 对 BPT 等价，5.4 日志实证引擎按同宽子类型读入
# （30 被报成 R8G8B8A8_UINT、58 被报成 R16_SNORM），不影响真值。
DXGI_R32G32B32A32_FLOAT = 2
DXGI_R16G16B16A16_FLOAT = 10
DXGI_R8G8B8A8_UNORM = 30
DXGI_R16_UNORM = 58
DXGI_R8_UNORM = 62

FORMATS = {
    DXGI_R32G32B32A32_FLOAT: ("R32G32B32A32_FLOAT", 16),
    DXGI_R16G16B16A16_FLOAT: ("R16G16B16A16_FLOAT", 8),
    DXGI_R8G8B8A8_UNORM: ("R8G8B8A8_UNORM", 4),
    DXGI_R16_UNORM: ("R16_UNORM", 2),
    DXGI_R8_UNORM: ("R8_UNORM", 1),
}

TRUTH_NAME = "dds_truth.json"


def _pixel_template(dxgi):
    """一个像素的字节模板（内容无所谓，但必须是该格式的合法字节数）。"""
    if dxgi == DXGI_R32G32B32A32_FLOAT:
        return struct.pack("<4f", 0.5, 0.25, 0.125, 1.0)
    if dxgi == DXGI_R16G16B16A16_FLOAT:
        return struct.pack("<4H", 0x3C00, 0x3800, 0x3000, 0x3C00)
    if dxgi == DXGI_R8G8B8A8_UNORM:
        return bytes((0x40, 0x80, 0xC0, 0xFF))
    if dxgi == DXGI_R16_UNORM:
        return struct.pack("<H", 0x2000)
    return bytes((0x7F,))


def _slice_bytes(width, height, dxgi):
    _name, bpt = FORMATS[dxgi]
    tpl = _pixel_template(dxgi)
    assert len(tpl) == bpt, (len(tpl), bpt)
    row = tpl * width
    assert len(row) == width * bpt
    return row * height


def _header(width, height, dxgi, faces, mips=1):
    _name, bpt = FORMATS[dxgi]
    cube = faces > 1
    pitch = width * bpt
    # 无损非体积图：PITCH 表明 dwPitchOrLinearSize 是行距，不置 DEPTH/LINEARSIZE
    flags = (DDSD_CAPS | DDSD_HEIGHT | DDSD_WIDTH | DDSD_PIXELFORMAT | DDSD_PITCH)
    if mips > 1:
        flags |= DDSD_MIPMAPCOUNT
    caps = DDSCAPS_TEXTURE
    if mips > 1:
        caps |= DDSCAPS_COMPLEX | DDSCAPS_MIPMAP
    caps2 = 0
    if cube:
        caps2 = DDSCAPS2_CUBEMAP | DDSCAPS2_CUBEMAP_ALL_FACES
        caps |= DDSCAPS_COMPLEX
    # DX10: FourCC='DX10'，其余 RGB 字段留 0
    pf = struct.pack("<II4sIIIII", 32, DDPF_FOURCC, b"DX10", 0, 0, 0, 0, 0)
    head = struct.pack(
        "<IIIIIIIII",
        124,                # dwSize
        flags,              # dwFlags
        height,             # dwHeight
        width,              # dwWidth
        pitch,              # dwPitchOrLinearSize
        1,                  # dwDepth
        mips,               # dwMipMapCount
        0,                  # dwReserved1[0]
        0,                  # dwReserved1[1]
    )
    head += struct.pack("<9I", 0, 0, 0, 0, 0, 0, 0, 0, 0)   # dwReserved1[2..10]
    head += pf
    head += struct.pack("<IIII", caps, caps2, 0, 0)          # dwCaps..dwCaps4
    head += struct.pack("<I", 0)                              # dwReserved2
    assert len(head) == 124, len(head)
    dx10 = struct.pack(
        "<IIIII",
        dxgi,
        DX10_RESOURCE_DIMENSION_TEXTURE2D,
        DX10_MISC_TEXTURECUBE if cube else 0,
        6 if cube else 1,
        DX10_MISC_FLAGS2_ALPHA_UNKNOWN,
    )
    assert len(dx10) == 20, len(dx10)
    return MAGIC + head + dx10


TARGETS = (
    # name, width, height, dxgi, faces  —— BPT 覆盖 16/8/4/2/1 + Cube
    ("T_CalibF32", 512, 512, DXGI_R32G32B32A32_FLOAT, 1),
    ("T_CalibF16", 512, 512, DXGI_R16G16B16A16_FLOAT, 1),
    ("T_CalibRGBA8", 1024, 1024, DXGI_R8G8B8A8_UNORM, 1),
    ("T_CalibR16", 1024, 1024, DXGI_R16_UNORM, 1),
    ("T_CalibR8", 1024, 1024, DXGI_R8_UNORM, 1),
    ("T_CalibCubeF32", 256, 256, DXGI_R32G32B32A32_FLOAT, 6),
)


def make_dds(out_dir, force=False):
    """写出 TARGETS 全部 DDS 与 truth.json，返回 truth dict（含文件路径与字节数）。"""
    os.makedirs(out_dir, exist_ok=True)
    truth = {"targets": [], "folder": "/Game/PrismCalib"}
    for name, w, h, dxgi, faces in TARGETS:
        fmt_name, bpt = FORMATS[dxgi]
        blob = _slice_bytes(w, h, dxgi)
        payload = blob * faces
        path = os.path.join(out_dir, name + ".dds")
        if force or (not os.path.isfile(path)) or os.path.getsize(path) == 0:
            with open(path, "wb") as f:
                f.write(_header(w, h, dxgi, faces) + payload)
        truth["targets"].append({
            "name": name,
            "path": path.replace("\\", "/"),
            "file_bytes": os.path.getsize(path),
            "authored_width": w,
            "authored_height": h,
            "authored_faces": faces,
            "authored_bpt": bpt,
            "authored_format": fmt_name,
            "dxgi": dxgi,
            "payload_bytes": len(payload),
            "mips": 1,
        })
    with open(os.path.join(out_dir, TRUTH_NAME), "w", encoding="utf-8") as f:
        json.dump(truth, f, indent=2, sort_keys=True)
    return truth


if __name__ == "__main__":
    import sys
    default = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_fixture_tmp", "dds")
    d = sys.argv[1] if len(sys.argv) > 1 else default
    t = make_dds(d)
    for item in t["targets"]:
        print(item["name"], item["authored_format"], item["authored_width"],
              "faces=%d" % item["authored_faces"], "%d bytes" % item["file_bytes"])
    print("truth ->", os.path.join(d, TRUTH_NAME))