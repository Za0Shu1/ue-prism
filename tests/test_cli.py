"""`prism setup` 安装器测试（无引擎）：toml/json 合并、幂等、卸载、保留用户条目。

对应 docs/DESIGN_v1.0.md §4。纯标准库 + tmp_path，不碰真实用户配置。
"""
from __future__ import annotations

import json

from prism import cli


# ---- toml (Codex) ----------------------------------------------------------

def test_toml_ensure_creates_block():
    out, changed = cli.toml_ensure("", "C:/py/python.exe")
    assert changed is True
    assert "[mcp_servers.ue-prism]" in out
    assert 'command = "C:/py/python.exe"' in out
    assert 'args = ["-m", "prism.server"]' in out


def test_toml_ensure_preserves_other_sections():
    base = "[other]\nkey = 1\n\n[mcp_servers.foo]\ncommand = \"x\"\n"
    out, changed = cli.toml_ensure(base, "PY")
    assert changed is True
    assert "[mcp_servers.foo]" in out
    assert "[other]" in out
    assert "key = 1" in out
    assert "[mcp_servers.ue-prism]" in out


def test_toml_ensure_idempotent():
    once, _ = cli.toml_ensure("", "PY")
    twice, changed = cli.toml_ensure(once, "PY")
    assert changed is False
    assert once == twice
    # only one header present
    assert twice.count("[mcp_servers.ue-prism]") == 1


def test_toml_remove_drops_only_target():
    base = "[mcp_servers.ue-prism]\ncommand = \"PY\"\nargs = []\n\n[mcp_servers.keep]\nc = 1\n"
    out, removed = cli.toml_remove(base)
    assert removed is True
    assert "ue-prism" not in out
    assert "[mcp_servers.keep]" in out
    assert "c = 1" in out


def test_toml_remove_missing_is_noop():
    base = "[mcp_servers.other]\nc = 1\n"
    out, removed = cli.toml_remove(base)
    assert removed is False
    assert out == base


# ---- json (opencode / cursor) ---------------------------------------------

def test_json_ensure_opencode_shape():
    out, changed, needs = cli.json_ensure("", "opencode", "PY")
    assert (changed, needs) == (True, False)
    obj = json.loads(out)
    entry = obj["mcp"]["ue-prism"]
    assert entry["type"] == "local"
    assert entry["command"] == ["PY", "-m", "prism.server"]
    assert entry["enabled"] is True


def test_json_ensure_cursor_shape():
    out, changed, needs = cli.json_ensure("", "cursor", "PY")
    obj = json.loads(out)
    entry = obj["mcpServers"]["ue-prism"]
    assert entry["command"] == "PY"
    assert entry["args"] == ["-m", "prism.server"]


def test_json_ensure_preserves_and_idempotent():
    base = json.dumps({"mcp": {"other": {"type": "local", "command": ["x"]}}, "theme": "d"})
    out1, changed1, _ = cli.json_ensure(base, "opencode", "PY")
    assert changed1 is True
    obj = json.loads(out1)
    assert "other" in obj["mcp"]
    assert obj["theme"] == "d"
    out2, changed2, _ = cli.json_ensure(out1, "opencode", "PY")
    assert changed2 is False
    assert out1 == out2


def test_json_remove():
    base = json.dumps({"mcpServers": {"ue-prism": {"command": "PY"}, "keep": {"command": "K"}}})
    out, removed, needs = cli.json_remove(base, "cursor")
    assert (removed, needs) == (True, False)
    obj = json.loads(out)
    assert "ue-prism" not in obj["mcpServers"]
    assert "keep" in obj["mcpServers"]


def test_json_needs_manual_on_unparsable():
    bad = "{ this is : not valid json // and has a comment"
    out, changed, needs = cli.json_ensure(bad, "cursor", "PY")
    assert needs is True
    assert out is None


def test_jsonc_comments_are_stripped():
    src = '{\n  // leading comment\n  "mcpServers": {} /* block */\n}'
    out, changed, needs = cli.json_ensure(src, "cursor", "PY")
    assert needs is False
    assert changed is True
    assert "ue-prism" in json.loads(out)["mcpServers"]


# ---- end-to-end setup_client (tmp files) ----------------------------------

def test_setup_client_codex_roundtrip(tmp_path):
    p = tmp_path / "config.toml"
    r1 = cli.setup_client("codex", py="PY", path=str(p))
    assert r1["written"] is True and r1["changed"] is True
    assert "[mcp_servers.ue-prism]" in p.read_text(encoding="utf-8")
    # second apply: no change
    r2 = cli.setup_client("codex", py="PY", path=str(p))
    assert r2["changed"] is False
    assert "written" not in r2
    # uninstall
    r3 = cli.setup_client("codex", py="PY", uninstall=True, path=str(p))
    assert r3["changed"] is True
    assert "ue-prism" not in p.read_text(encoding="utf-8")


def test_setup_client_json_with_existing(tmp_path):
    p = tmp_path / "mcp.json"
    p.write_text(json.dumps({"mcpServers": {"keep": {"command": "z"}}}), encoding="utf-8")
    r = cli.setup_client("cursor", py="PY", path=str(p))
    assert r["written"] is True
    obj = json.loads(p.read_text(encoding="utf-8"))
    assert "keep" in obj["mcpServers"] and "ue-prism" in obj["mcpServers"]


def test_setup_client_dry_run_writes_nothing(tmp_path):
    p = tmp_path / "mcp.json"
    r = cli.setup_client("cursor", py="PY", dry_run=True, path=str(p))
    assert r["dry_run"] is True and not p.exists()


def test_setup_client_claude_desktop_uses_mcpservers(tmp_path):
    p = tmp_path / "claude_desktop_config.json"
    r = cli.setup_client("claude", py="PY", path=str(p))
    assert r["written"] is True
    obj = json.loads(p.read_text(encoding="utf-8"))
    assert obj["mcpServers"]["ue-prism"]["command"] == "PY"
    assert obj["mcpServers"]["ue-prism"]["args"] == ["-m", "prism.server"]


def test_default_path_claude_is_desktop_config():
    import os
    from pathlib import Path
    path = cli.default_path("claude")
    assert path.name == "claude_desktop_config.json"


def test_setup_client_claude_code_uses_stdio_type(tmp_path):
    p = tmp_path / ".mcp.json"
    r = cli.setup_client("claude-code", py="PY", path=str(p))
    assert r["written"] is True
    obj = json.loads(p.read_text(encoding="utf-8"))
    entry = obj["mcpServers"]["ue-prism"]
    assert entry["type"] == "stdio"
    assert entry["command"] == "PY"
    assert entry["args"] == ["-m", "prism.server"]
