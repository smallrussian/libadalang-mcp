"""Protocol tests: spawn the server and talk to it with the SDK client.

These run without libadalang: a stub package on PYTHONPATH shadows it.
"""

from __future__ import annotations

import json
import os

import anyio
import pytest

from conftest import FIXTURE_GPR, have_libadalang, mcp_client

needs_lal = pytest.mark.skipif(not have_libadalang(), reason="libadalang not importable")

ALLOWED_TYPES = {"string", "integer", "boolean"}
FORBIDDEN_KEYS = {"oneOf", "anyOf", "allOf", "$ref", "$defs", "format", "items"}


def run(coro_fn):
    return anyio.run(coro_fn)


def test_initialize_and_list_tools_without_libadalang():
    async def go():
        async with mcp_client(["-P", FIXTURE_GPR], stub="no_lal") as (s, init):
            assert init.server_info.name == "lal-mcp"
            assert "verify before asserting" in (init.instructions or "").lower()
            tools = (await s.list_tools()).tools
            return init, tools

    init, tools = run(go)
    for t in tools:
        schema = t.input_schema
        assert schema["type"] == "object", t.name
        assert not (FORBIDDEN_KEYS & set(schema)), t.name
        for pname, prop in schema.get("properties", {}).items():
            assert prop.get("type") in ALLOWED_TYPES, (t.name, pname, prop)
            assert prop.get("description"), (t.name, pname)
            assert not (FORBIDDEN_KEYS & set(prop)), (t.name, pname, prop)
        desc = t.description or ""
        assert desc and desc.count(". ") <= 1, (t.name, desc)


def test_tool_call_without_libadalang_is_error_result():
    async def go():
        async with mcp_client(["-P", FIXTURE_GPR], stub="no_lal") as (s, _):
            r1 = await s.call_tool("find_unit", {"name": "Vehicle"})
            r2 = await s.call_tool("outline", {"target": "Vehicle"})
            return r1, r2

    r1, r2 = run(go)
    for r in (r1, r2):
        assert r.is_error
        assert "libadalang import failed" in r.content[0].text


def test_native_stdout_writes_during_load_do_not_break_session():
    """The stub writes to fd 1 / the Win32 stdout handle / print() while 'loading'."""

    async def go():
        async with mcp_client(["-P", FIXTURE_GPR], stub="noisy_lal") as (s, _):
            r = await s.call_tool("find_unit", {"name": "Vehicle"})
            tools = (await s.list_tools()).tools  # session still alive and in sync
            return r, tools

    r, tools = run(go)
    assert r.is_error and "noisy stub" in r.content[0].text
    assert len(tools) >= 5


def test_bad_arguments_are_error_results():
    async def go():
        async with mcp_client(["-P", FIXTURE_GPR], stub="no_lal") as (s, _):
            return await s.call_tool("goto_definition", {"file": "x.adb", "line": "not a number", "column": 1})

    r = run(go)
    assert r.is_error


@pytest.mark.skipif(not __import__("conftest").have_libadalang(), reason="libadalang not importable")
def test_real_tool_call_over_stdio():
    async def go():
        async with mcp_client(["-P", FIXTURE_GPR, "-XBUILD=release", "--max-chars", "300"]) as (s, _):
            r1 = await s.call_tool("goto_definition", {"file": "src/main_proc.2.ada", "line": 14, "column": 4})
            r2 = await s.call_tool("find_references", {"file": "src/vehicle__engine.1.ada", "line": 2,
                                                        "column": 17, "limit": 100})
            return r1, r2

    r1, r2 = run(go)
    assert not r1.is_error
    assert "procedure Log (Value : Integer);" in r1.content[0].text
    assert len(r2.content[0].text) <= 300 and "[truncated:" in r2.content[0].text


def test_bad_cli_args_exit_nonzero():
    import subprocess
    import sys

    from conftest import SERVER

    r = subprocess.run([sys.executable, SERVER, "-X", "NO_EQUALS_SIGN"], stdin=subprocess.DEVNULL,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode != 0
    assert r.stdout == ""  # usage errors go to stderr, never the protocol stream
    assert "VAR=VALUE" in r.stderr


def test_server_survives_missing_path_env():
    """GPR's Ada code aborts the process when PATH is unset; the server must not die."""
    import json
    import subprocess
    import sys

    from conftest import ROOT, SERVER, server_env

    env = server_env(drop=("PATH",))
    p = subprocess.Popen([sys.executable, SERVER, "-P", FIXTURE_GPR], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env, cwd=ROOT)

    def rpc(i, method, params=None):
        msg = {"jsonrpc": "2.0", "id": i, "method": method, "params": params or {}}
        p.stdin.write((json.dumps(msg) + "\n").encode())
        p.stdin.flush()
        while True:
            line = p.stdout.readline()
            assert line, "server died"
            resp = json.loads(line)
            if resp.get("id") == i:
                return resp

    try:
        rpc(1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                              "clientInfo": {"name": "t", "version": "0"}})
        p.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
        p.stdin.flush()
        r = rpc(2, "tools/call", {"name": "find_unit", "arguments": {"name": "Vehicle"}})
        assert "result" in r  # an answer (or an isError result), not a dead process
        assert "result" in rpc(3, "tools/list")
    finally:
        p.stdin.close()
        p.wait(timeout=30)


@pytest.mark.skipif(not __import__("conftest").have_libadalang(), reason="libadalang not importable")
def test_run_lal_over_stdio_timeout_and_fd1_writes():
    async def go():
        async with mcp_client(["-P", FIXTURE_GPR, "--timeout", "2"]) as (s, _):
            r1 = await s.call_tool("run_lal", {"code": "while True:\n    pass"})
            r2 = await s.call_tool("run_lal", {"code": "import os\nos.write(1, b'RAW FD1 WRITE' + bytes([10]))\nprint('ok')"})
            r3 = await s.call_tool("run_lal", {"code": 'rel(unit("Vehicle").root)'})
            r4 = await s.call_tool("api_help", {"name": "WithClause"})
            return r1, r2, r3, r4

    r1, r2, r3, r4 = run(go)
    assert r1.is_error and "timed out after 2s" in r1.content[0].text
    assert not r2.is_error and r2.content[0].text.strip() == "ok"
    assert r3.content[0].text.strip() == "'src/vehicle.1.ada:2'"
    assert not r4.is_error


# ------------------------------------------------- project discovery (no -P)


def _write_config(d, extra: str = "") -> None:
    (d / ".lal-mcp.toml").write_text(f"project = {json.dumps(FIXTURE_GPR)}\n{extra}", encoding="utf-8")


def test_not_configured_is_an_error_until_config_appears(tmp_path):
    async def go():
        async with mcp_client([], cwd=str(tmp_path), stub="no_lal") as (s, _):
            r1 = await s.call_tool("find_unit", {"name": "Vehicle"})
            _write_config(tmp_path)
            r2 = await s.call_tool("find_unit", {"name": "Vehicle"})  # retried, not cached
            return r1, r2

    r1, r2 = run(go)
    assert r1.is_error and "not configured" in r1.content[0].text and ".lal-mcp.toml" in r1.content[0].text
    assert r2.is_error and "libadalang import failed" in r2.content[0].text  # got past configuration


@needs_lal
def test_config_found_from_server_cwd_subdir(tmp_path):
    fixture_dir = json.dumps(os.path.dirname(FIXTURE_GPR))
    _write_config(tmp_path, f'workspace = {fixture_dir}\n[scenario]\nBUILD = "release"\n')
    sub = tmp_path / "deep" / "er"
    sub.mkdir(parents=True)

    async def go():
        async with mcp_client([], cwd=str(sub)) as (s, _):
            return await s.call_tool("find_unit", {"name": "Vehicle.Engine"})

    r = run(go)
    text = r.content[0].text
    assert not r.is_error, text
    assert text.startswith("[project: fixture.gpr from ") and ".lal-mcp.toml" in text
    assert "spec     src/vehicle__engine.1.ada" in text


@needs_lal
def test_client_roots_win_over_server_cwd(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    _write_config(proj)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    async def go():
        async with mcp_client([], cwd=str(elsewhere), roots=[str(proj)]) as (s, _):
            return await s.call_tool("outline", {"target": "Vehicle"})

    r = run(go)
    assert not r.is_error, r.content[0].text
    assert "client workspace root" in r.content[0].text.splitlines()[0]


@needs_lal
def test_single_gpr_in_root_is_used_and_announced():
    async def go():
        async with mcp_client([], cwd=os.path.dirname(FIXTURE_GPR)) as (s, _):
            return await s.call_tool("find_unit", {"name": "Utils"})

    r = run(go)
    first = r.content[0].text.splitlines()[0]
    assert not r.is_error
    assert "the only .gpr" in first and "no .lal-mcp.toml" in first


def test_tool_annotations_mark_read_only_and_run_lal():
    """Clients (e.g. Codex) auto-approve read-only tools; run_lal executes code, so it must not claim that."""
    async def go():
        async with mcp_client(["-P", FIXTURE_GPR], stub="no_lal") as (s, _):
            return (await s.list_tools()).tools

    ann = {t.name: t.annotations for t in run(go)}
    assert ann["run_lal"].read_only_hint is False and ann["run_lal"].destructive_hint is True
    for name, a in ann.items():
        if name != "run_lal":
            assert a.read_only_hint is True and a.destructive_hint is False, name
