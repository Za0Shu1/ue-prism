# -*- coding: utf-8 -*-
"""#11 C 探针判据（scripts/calib_fixture/source_scan.py）的离线用例。

探针在引擎里只负责"把 uasset 字节喂给 scan"，判据全在这个纯 stdlib 模块——
所以它的正确性（能认出真头）与保守性（噪声不许误报）都能离线钉死。
"""
import importlib.util
import os
import random
import struct

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_SCAN = os.path.join(_REPO, "scripts", "calib_fixture", "source_scan.py")
_MAKE_DDS = os.path.join(_REPO, "scripts", "calib_fixture", "make_dds.py")


def _load(path, modname):
    spec = importlib.util.spec_from_file_location(modname, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ss = _load(_SCAN, "source_scan")
dds = _load(_MAKE_DDS, "make_dds_for_scan")


def _png(side_w, side_h, bitdepth=8, colortype=6):
    ihdr = b"\x00\x00\x00\x0d" + b"IHDR" + struct.pack(">II", side_w, side_h)
    ihdr += bytes([bitdepth, colortype, 0, 0, 0])
    ihdr += b"\x00\x00\x00\x00"  # 伪 CRC，parser 不校验
    return b"\x89PNG\r\n\x1a\n" + ihdr


def _jpeg(w, h):
    sof = b"\xff\xc0" + struct.pack(">H", 17) + b"\x08"
    sof += struct.pack(">HH", h, w) + b"\x03" + b"\x00\x22\x00"
    return b"\xff\xd8\xff\xe0\x00\x0c" + b"JFIF\x00" + b"\x01\x02" + b"\x00" * 3 + sof


def _bmp(w, h, bpp=24):
    row = ((w * bpp + 31) // 32) * 4
    file_size = 14 + 40 + row * h
    head = b"BM" + struct.pack("<IHHI", file_size, 0, 0, 54)
    dib = struct.pack("<IiiHHIIiiII", 40, w, h, 1, bpp, 0, row * h, 0, 0, 0, 0)
    return head + dib + b"\x00" * (row * h)


def test_scan_finds_each_container_head():
    blob = _png(512, 256)
    hits = [h for h in ss.scan_blob(blob) if h["kind"] == "png"]
    assert len(hits) == 1 and (hits[0]["w"], hits[0]["h"]) == (512, 256)
    hits = [h for h in ss.scan_blob(_jpeg(1024, 768)) if h["kind"] == "jpeg"]
    assert len(hits) == 1 and (hits[0]["w"], hits[0]["h"]) == (1024, 768)
    hits = [h for h in ss.scan_blob(_bmp(64, 64)) if h["kind"] == "bmp"]
    assert len(hits) == 1 and (hits[0]["w"], hits[0]["h"]) == (64, 64)


def test_bmp_rejects_self_inconsistent_file_size():
    blob = bytearray(_bmp(64, 64))
    struct.pack_into("<I", blob, 2, 1 << 30)          # 声明尺寸远超缓冲
    assert [h for h in ss.scan_blob(bytes(blob)) if h["kind"] == "bmp"] == []


def test_png_truncation_is_boundsafe_and_silent():
    assert ss.scan_blob(_png(512, 512)[:20]) == []
    assert ss.scan_blob(b"") == []


def test_random_noise_must_not_produce_false_hits():
    rng = random.Random(20261015)
    noise = bytes(rng.randrange(256) for _ in range(65536))
    assert ss.scan_blob(noise) == []


def test_own_dds_files_scan_to_exactly_authored_dims(tmp_path):
    """B1 靶文件是现成的真值：扫描必须每张恰好 1 个 DDS 命中且宽高/面数对上。

    这条同时验证 parse_dds 的二次校验（payload 梯度噪声不得误报）。
    """
    truth = dds.make_dds(str(tmp_path))
    for item in truth["targets"]:
        data = open(item["path"], "rb").read()
        hits = [h for h in ss.scan_blob(data) if h["kind"] == "dds"]
        assert len(hits) == 1, item["name"]
        assert hits[0]["w"] == item["authored_width"] and hits[0]["h"] == item["authored_height"]
        if item["authored_faces"] == 6:
            assert hits[0]["array_size"] == 6 and hits[0]["resource_dimension"] == 3
        assert [h for h in hits if h["kind"] != "dds"] == []


def test_name_token_counter():
    blob = b"xx" + b"SourceFileData" + b"yy" + b"AssetImportData" + b"SourceFileData"
    got = ss.scan_names(blob)
    assert got["SourceFileData"] == 2 and got["AssetImportData"] == 1
    assert got["TextureSourceData"] == 0