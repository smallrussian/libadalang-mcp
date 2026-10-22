from __future__ import annotations

import os
import sys
from contextlib import asynccontextmanager

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS = os.path.join(ROOT, "tests")
FIXTURE = os.path.join(TESTS, "fixture")
FIXTURE_GPR = os.path.join(FIXTURE, "fixture.gpr")
SERVER = os.path.join(ROOT, "lal_mcp", "server.py")

if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def server_env(stub: str | None = None) -> dict[str, str]:
    """Full environment for the server process, optionally shadowing libadalang with a stub."""
    env = dict(os.environ)
    if stub:
        stub_dir = os.path.join(TESTS, "stubs", stub)
        env["PYTHONPATH"] = stub_dir + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONIOENCODING"] = "utf-8"
    return env


@asynccontextmanager
async def mcp_client(args: list[str], stub: str | None = None):
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    params = StdioServerParameters(command=sys.executable, args=[SERVER, *args], env=server_env(stub), cwd=ROOT)
    with open(os.devnull, "w") as devnull:
        async with stdio_client(params, errlog=devnull) as (read, write):
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                yield session, init


def have_libadalang() -> bool:
    try:
        import libadalang  # noqa: F401
    except Exception:
        return False
    return True


requires_lal = pytest.mark.skipif(not have_libadalang(), reason="libadalang not importable")
