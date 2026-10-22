"""lal-mcp: stdio MCP server exposing Libadalang for an Ada codebase.

Run as `python lal_mcp/server.py -P project.gpr ...` or `python -m lal_mcp ...`.
Libadalang is imported lazily on the first tools/call, and `main()` moves fd 1
out of the way before anything else, so native Libadalang/GPR code can never
write into the JSON-RPC stream.
"""

from __future__ import annotations

import os
import sys

if __package__ in (None, ""):  # run as a script: make `lal_mcp` importable
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import argparse
import logging
from typing import Any, Callable

import anyio
from mcp.server.mcpserver import MCPServer
from mcp.server.stdio import stdio_server
from mcp_types import CallToolResult, TextContent

from lal_mcp.session import Config, Session, ToolFailure, cap, last_line
from lal_mcp.stdio_guard import protect_stdio

log = logging.getLogger("lal_mcp")

INSTRUCTIONS = """\
Ada code intelligence backed by Libadalang name resolution over the project's own GPR file.
Verify before asserting: check a subprogram's signature, a type's components, or who calls
something with these tools before stating it or editing code that depends on it.
"unresolved" and "[approximate]" in a result are unknowns: say so, do not fill them in.
Every location is path:line relative to the workspace root; quote declaration text as shown.
Prefer outline / package_files / goto_definition over reading whole files.
"""

SESSION: Session | None = None


def call(fn: Callable[..., tuple[str, bool]], *args: Any, hint: str = "narrow the query") -> CallToolResult:
    """Run a tool body against the warm session; every failure becomes an isError result."""
    max_chars = SESSION.cfg.max_chars if SESSION else 8000
    try:
        if SESSION is None:
            raise ToolFailure("server not configured (no project)")
        SESSION.ensure_loaded()
        text, is_error = SESSION.with_retry(lambda: fn(SESSION, *args))
        if SESSION.load_note:
            text, SESSION.load_note = SESSION.load_note + text, None
    except ToolFailure as exc:
        text, is_error = str(exc), True
    except Exception as exc:  # PropertyError, repeated StaleReferenceError, bugs
        log.exception("tool failed")
        text, is_error = f"error: {last_line(exc)}", True
    return CallToolResult(content=[TextContent(type="text", text=cap(text, max_chars, hint))], is_error=is_error)


def build_app() -> MCPServer:
    from lal_mcp.tools import register

    app = MCPServer(name="lal-mcp", instructions=INSTRUCTIONS, version="0.1.0")
    register(app, call)
    return app


def parse_args(argv: list[str] | None = None) -> Config:
    p = argparse.ArgumentParser(prog="lal-mcp", description="Libadalang MCP server (stdio)")
    p.add_argument("-P", "--project", required=True, help="root .gpr project file")
    p.add_argument("-w", "--workspace", help="workspace root for relative paths (default: project dir)")
    p.add_argument("-X", dest="scenario", action="append", default=[], metavar="VAR=VALUE",
                   help="scenario variable (repeatable)")
    p.add_argument("--target", help="GPR target")
    p.add_argument("--RTS", dest="runtime", help="Ada runtime")
    p.add_argument("--max-chars", type=int, default=8000, help="hard cap per response (default 8000)")
    p.add_argument("--timeout", type=int, default=20, help="run_lal timeout in seconds (default 20)")
    p.add_argument("--log-level", default="INFO", help="stderr log level")
    a = p.parse_args(argv)
    scenario: dict[str, str] = {}
    for item in a.scenario:
        if "=" not in item:
            p.error(f"-X expects VAR=VALUE, got {item!r}")
        k, v = item.split("=", 1)
        scenario[k] = v
    logging.basicConfig(stream=sys.stderr, level=a.log_level.upper(),
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    return Config(project=a.project, workspace=a.workspace, scenario=scenario, target=a.target,
                  runtime=a.runtime, max_chars=a.max_chars, timeout=a.timeout)


async def _serve(app: MCPServer, wire_in, wire_out) -> None:
    # Explicit streams: the transport uses only our private duplicates of the real pipes.
    async with stdio_server(stdin=anyio.wrap_file(wire_in), stdout=anyio.wrap_file(wire_out)) as (read_stream, write_stream):
        low = app._lowlevel_server  # same as MCPServer.run_stdio_async (mcp==2.3.0)
        await low.run(read_stream, write_stream, low.create_initialization_options())


def main(argv: list[str] | None = None) -> None:
    global SESSION
    wire_in, wire_out = protect_stdio()  # first: native code must never touch the real pipes
    cfg = parse_args(argv)
    SESSION = Session(cfg)
    app = build_app()
    log.info("lal-mcp starting: project=%s workspace=%s (lazy load)", SESSION.project_path, SESSION.workspace)
    anyio.run(_serve, app, wire_in, wire_out)


if __name__ == "__main__":
    main()
