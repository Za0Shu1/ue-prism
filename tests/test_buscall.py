"""bus_call 参数解析测试（跨 shell 友好：key=value + JSON）。"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import bus_call  # noqa: E402


def test_kv_and_coercion():
    d = bus_call.parse_args(["limit=30", "class_contains=Skeletal", "dry=false", "only_none=none"])
    assert d == {"limit": 30, "class_contains": "Skeletal", "dry": False, "only_none": None}


def test_json_string():
    assert bus_call.parse_args(['{"limit": 5}']) == {"limit": 5}


def test_empty():
    assert bus_call.parse_args([]) == {}


def test_bad_token_raises():
    try:
        bus_call.parse_args(["oops"])
        assert False, "expected ValueError"
    except ValueError:
        pass