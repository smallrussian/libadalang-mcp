"""--http mode: the client connects by URL instead of launching the server."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from contextlib import asynccontextmanager, contextmanager

import anyio
import pytest

from conftest import FIXTURE_GPR, ROOT, SERVER, have_libadalang, server_env

needs_lal = pytest.mark.skipif(not have_libadalang(), reason="libadalang not importable")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextmanager
def http_server(args: list[str], stub: str | None = None, env_extra: dict | None = None, cwd: str = ROOT):
    port = _free_port()
    env = server_env(stub)
    env.update(env_extra or {})
    p = subprocess.Popen([sys.executable, SERVER, "--http", "--port", str(port), *args], cwd=cwd, env=env,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if p.poll() is not None:
                raise AssertionError(f"server exited early: {p.stderr.read().decode(errors='replace')[-800:]}")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                    break
            except OSError:
                time.sleep(0.2)
        else:
            raise AssertionError("server did not start listening")
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        p.terminate()
        try:
            p.wait(timeout=15)
        except subprocess.TimeoutExpired:
            p.kill()


@asynccontextmanager
async def http_client(url: str, headers: dict | None = None):
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared._httpx_utils import create_mcp_http_client

    async with create_mcp_http_client(headers=headers) as http:
        async with streamable_http_client(url, http_client=http) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session


def test_http_serves_tools_without_libadalang():
    with http_server(["-P", FIXTURE_GPR], stub="no_lal") as url:
        async def go():
            async with http_client(url) as s:
                tools = (await s.list_tools()).tools
                r = await s.call_tool("find_unit", {"name": "Vehicle"})
                return tools, r

        tools, r = anyio.run(go)
    assert {t.name for t in tools} >= {"find_unit", "outline", "goto_definition", "run_lal"}
    assert r.is_error and "libadalang import failed" in r.content[0].text


@needs_lal
def test_http_real_answers_and_project_from_cwd_config(tmp_path):
    (tmp_path / ".lal-mcp.toml").write_text(
        f'project = "{FIXTURE_GPR.replace(chr(92), "/")}"\nworkspace = "{os.path.dirname(FIXTURE_GPR).replace(chr(92), "/")}"\n')
    with http_server([], cwd=str(tmp_path)) as url:
        async def go():
            async with http_client(url) as s:
                a = await s.call_tool("goto_definition", {"file": "src/main_proc.2.ada", "line": 14, "column": 4})
                b = await s.call_tool("run_lal", {"code": 'rel(unit("Vehicle").root)'})
                return a, b

        a, b = anyio.run(go)
    assert not a.is_error and "procedure Log (Value : Integer);" in a.content[0].text
    assert b.content[0].text.strip() == "'src/vehicle.1.ada:2'"


def test_http_bearer_token():
    env = {"LAL_TEST_TOKEN": "s3cret"}
    with http_server(["-P", FIXTURE_GPR, "--token-env", "LAL_TEST_TOKEN"], stub="no_lal", env_extra=env) as url:
        req = urllib.request.Request(url, data=b"{}", method="POST", headers={"Content-Type": "application/json"})
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req, timeout=10)
        assert exc.value.code == 401

        async def go():
            async with http_client(url, headers={"Authorization": "Bearer s3cret"}) as s:
                return (await s.list_tools()).tools

        assert anyio.run(go)


def _run_cli(args: list[str], cwd: str = ROOT, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    env = server_env("no_lal")
    env.update(env_extra or {})
    return subprocess.run([sys.executable, SERVER, *args], cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, timeout=60)


def test_http_refuses_non_loopback_without_token():
    r = _run_cli(["--http", "--host", "0.0.0.0", "-P", FIXTURE_GPR])
    assert r.returncode != 0 and "--token-env" in r.stderr and r.stdout == ""


def test_http_token_env_must_be_set():
    r = _run_cli(["--http", "--token-env", "LAL_SURELY_UNSET_VAR", "-P", FIXTURE_GPR])
    assert r.returncode != 0 and "LAL_SURELY_UNSET_VAR" in r.stderr


def test_http_not_configured_exits_at_startup(tmp_path):
    r = _run_cli(["--http", "--port", str(_free_port())], cwd=str(tmp_path))
    assert r.returncode == 2 and "not configured" in r.stderr
