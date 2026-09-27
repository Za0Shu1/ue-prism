# -*- coding: utf-8 -*-
"""校准夹具源图生成（纯代码、可复跑）：4K 噪声 x2（真大/假大同源）+ 1K + 256 孤儿 x3。

用法: python make_pngs.py [输出目录]   默认 <repo>/_fixture_tmp/pngs
"""
import os
import sys

from PIL import Image


def make_pngs(out_dir):
    os.makedirs(out_dir, exist_ok=True)
    try:
        import numpy as np
        rng = np.random.default_rng(7)
        def noise(w, h):
            return Image.fromarray(
                rng.integers(0, 256, size=(h, w, 3), dtype=np.uint8), "RGB")
    except Exception:
        import hashlib
        def noise(w, h):
            seed = (w * 100000 + h).to_bytes(8, "little")
            buf = bytearray()
            i = 0
            while len(buf) < w * h * 3:
                buf += hashlib.sha512(seed + str(i).encode()).digest()
                i += 1
            return Image.frombytes("RGB", (w, h), bytes(buf[:w * h * 3]))
    files = {"T_BigNoise": (4096, 4096), "T_Used1K": (1024, 1024),
             "T_Orphan1": (256, 256), "T_Orphan2": (256, 256), "T_Orphan3": (256, 256)}
    out = []
    for name, (w, h) in sorted(files.items()):
        p = os.path.join(out_dir, name + ".png")
        if not os.path.isfile(p) or os.path.getsize(p) == 0:
            noise(w, h).save(p, compress_level=1)
        out.append(p)
    return out


if __name__ == "__main__":
    default = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_fixture_tmp", "pngs")
    d = sys.argv[1] if len(sys.argv) > 1 else default
    for p in make_pngs(d):
        print(p, os.path.getsize(p))