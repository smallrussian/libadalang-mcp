"""Offline tests for the agent A/B harness (no agent, no libadalang needed)."""

from __future__ import annotations

import json
import os
import sys
import tomllib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import eval_agent as ea  # noqa: E402

ADA = {"command": "/venv/bin/python", "args": ["/x/server.py", "-P", "/ws/a b.gpr", "-XOS=unix"],
       "env": {"PATH": "/opt/gnat/bin:/usr/bin"}}
BASE = {"model_provider": "proxy", "model": "gpt-x",
        "model_providers": {"proxy": {"name": "internal proxy", "base_url": "https://p/v1",
                                      "env_key": "PROXY_API_KEY", "wire_api": "responses"}},
        "mcp_servers": {"leak": {"command": "nope"}}}


def _as_config(overrides: list[str]) -> dict:
    """Rebuild the config Codex would see from `-c key=value` pairs (keys may be dotted)."""
    cfg: dict = {}
    pairs = overrides[1::2]
    assert overrides[0::2] == ["-c"] * len(pairs)
    for p in pairs:
        key, value = p.split("=", 1)
        parsed = tomllib.loads(f"v = {value}")["v"]  # must be valid TOML
        node = cfg
        parts = key.split(".")
        for k in parts[:-1]:
            node = node.setdefault(k, {})
        node[parts[-1]] = parsed
    return cfg


def test_codex_overrides_with_tools_round_trip():
    cfg = _as_config(ea.codex_overrides(BASE, ADA))
    assert cfg["model_provider"] == "proxy" and cfg["model"] == "gpt-x"
    assert cfg["model_providers"]["proxy"]["base_url"] == "https://p/v1"
    assert cfg["approval_policy"] == "never"
    ada = cfg["mcp_servers"]["ada"]
    assert ada["command"] == ADA["command"] and ada["args"] == ADA["args"] and ada["env"] == ADA["env"]
    assert "leak" not in cfg["mcp_servers"]  # servers from the eval TOML are never passed


def test_codex_overrides_no_tools_has_no_servers():
    cfg = _as_config(ea.codex_overrides(BASE, None))
    assert "mcp_servers" not in cfg


def test_toml_value_escapes():
    v = 'C:\\GNAT\\bin;"quoted"'
    assert tomllib.loads(f"v = {ea.toml_value(v)}")["v"] == v


def test_parse_codex_events():
    events = [
        {"type": "thread.started", "thread_id": "t"},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"id": "1", "type": "reasoning", "text": "..."}},
        {"type": "item.completed", "item": {"id": "2", "type": "mcp_tool_call", "server": "ada",
                                            "tool": "find_references", "status": "completed"}},
        {"type": "item.completed", "item": {"id": "3", "type": "command_execution",
                                            "command": "rg -n Create_Object core/src", "exit_code": 0}},
        {"type": "item.completed", "item": {"id": "4", "type": "agent_message", "text": "a.adb:1"}},
        {"type": "turn.completed", "usage": {"input_tokens": 1200, "cached_input_tokens": 800,
                                             "output_tokens": 90}},
    ]
    out = "\n".join(json.dumps(e) for e in events) + "\nnot json\n"
    ev = ea.parse_codex_events(out)
    assert ev["tools"] == {"mcp:ada.find_references": 1, "shell:rg": 1}
    assert ev["usage"] == {"input": 1200, "cached": 800, "output": 90}
    assert ev["failed"] is None and ev["turns"] == 1


def test_parse_codex_events_failure():
    ev = ea.parse_codex_events(json.dumps({"type": "turn.failed", "error": {"message": "401"}}))
    assert ev["failed"] and "401" in ev["failed"]


def test_score_groups_and_wrong_locations():
    q = {"expected": ["a.ads:10"], "expected_groups": [["a.ads:10", "a.adb:50"]], "fp_files": ["a.ads", "a.adb"]}
    assert ea.score(q, "core/src/a.adb:50")["correct"]           # body of the right subprogram
    r = ea.score(q, "core/src/a.ads:10\ncore/src/a.ads:20")       # plus another overload
    assert not r["correct"] and r["wrong"] == ["a.ads:20"]
    q2 = {"expected": ["x.adb:3", "x.adb:4"], "fp_files": ["x.adb"]}
    assert ea.score(q2, "x.adb:3-4")["correct"]                   # ranges count
    assert ea.score(q2, "x.adb:3")["recall"] == 0.5
