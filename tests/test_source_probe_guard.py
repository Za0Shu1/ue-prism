# -*- coding: utf-8 -*-
"""源尺寸探针的崩溃护栏回归（5.4 真实工程把编辑器打出引擎断言）。

复现签名：批量改 MaxTextureSize 会异步重压缩贴图，此时调
`blueprint_get_texture_source_disk_and_memory_size` 走 FTextureSource::CalcMipSize，
Mip 与 Block.NumMips 瞬时不一致 -> `Assertion failed: MipIndex < Block.NumMips
(Texture.cpp:2722)`，Python 的 try 拦不住断言。护栏 = 同一路径只探一次（会话缓存）
+ 批量写场景可 probe_source=False 彻底绕开，且关掉后必须显式播报。
"""
from __future__ import annotations

import pytest

from prism.domain import metrics as M


class _Tex:
    """最小纹理形状：只实现度量链会用到的方法，并统计引擎探针调用次数。"""

    def __init__(self, path="/Game/T/X.X", disk=4096, mem=8388608, raises=False):
        self._path = path
        self._pair = (disk, mem)
        self._raises = raises
        self.probe_calls = 0

    def get_path_name(self):
        return self._path

    def blueprint_get_size_x(self):
        return 2048

    def blueprint_get_size_y(self):
        return 2048

    def blueprint_get_memory_size(self):
        return 4194304

    def blueprint_get_texture_source_disk_and_memory_size(self):
        self.probe_calls += 1
        if self._raises:
            raise RuntimeError("boom")
        return self._pair


@pytest.fixture(autouse=True)
def _clean_cache():
    M.reset_source_probe_cache()
    yield
    M.reset_source_probe_cache()


def test_source_probe_is_called_once_per_path():
    tex = _Tex()
    tried = []
    first = M._source_probe(tex, tried)
    second = M._source_probe(tex, tried)
    assert first == (4096, 8388608) and second == first
    assert tex.probe_calls == 1, tex.probe_calls
    assert "blueprint_get_texture_source_disk_and_memory_size" in tried
    assert "blueprint_get_texture_source_disk_and_memory_size:cached" in tried


def test_probe_source_off_never_touches_the_risky_api():
    tex = _Tex()
    tried = []
    assert M._source_probe(tex, tried, probe_source=False) is None
    assert tex.probe_calls == 0
    assert "source_probe:disabled" in tried


def test_failed_probe_is_cached_and_not_retried():
    tex = _Tex(raises=True)
    tried = []
    assert M._source_probe(tex, tried) is None
    assert M._source_probe(tex, tried) is None
    assert tex.probe_calls == 1, "探针失败也必须只撞一次，别把竞态窗口反复点开"
    assert "blueprint_get_texture_source_disk_and_memory_size:failed_cached" in tried


def test_measure_texture_without_probe_keeps_runtime_fields():
    tex = _Tex()
    tried = []
    m = M._measure_texture(None, tex, tried, probe_source=False)
    assert m["width"] == 2048 and m["memory_bytes"] == 4194304
    assert "source_memory_bytes" not in m and "source_disk_bytes" not in m
    assert tex.probe_calls == 0


def test_measure_texture_with_probe_fills_source_ledger():
    tex = _Tex()
    tried = []
    m = M._measure_texture(None, tex, tried, probe_source=True)
    assert m["source_disk_bytes"] == 4096 and m["source_memory_bytes"] == 8388608


def test_reset_cache_allows_reprobe():
    tex = _Tex()
    M._source_probe(tex, [])
    M.reset_source_probe_cache()
    M._source_probe(tex, [])
    assert tex.probe_calls == 2
