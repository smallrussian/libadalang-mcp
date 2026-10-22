"""Protocol tests: spawn the server and talk to it with the SDK client.

These run without libadalang: a stub package on PYTHONPATH shadows it.
"""

from __future__ import annotations

import anyio
import pytest

from conftest import FIXTURE_GPR, mcp_client

ALLOWED_TYPES = {"string", "integer", "boolean"}
FORBIDDEN_KEYS = {"oneOf", "anyOf", "allOf", "$ref", "$defs", "format", "items"}


def run(coro_fn):
    return anyio.run(coro_fn)


def test_initialize_and_list_tools_without_libadalang():
    async def go():
        async with mcp_client(["-P", FIXTURE_GPR], stub="no_lal") as (s, init):
            assert init.server_info.name == "lal-mcp"
            assert "Verify before asserting" in (init.instructions or "")
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


def test_bad_cli_args_exit_nonzero():
    import subprocess
    import sys

    from conftest import SERVER

    r = subprocess.run([sys.executable, SERVER], capture_output=True, text=True, timeout=60)
    assert r.returncode != 0
    assert r.stdout == ""  # usage errors go to stderr, never the protocol stream
    assert "--project" in r.stderr
