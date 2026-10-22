"""lal-mcp: stdio MCP server exposing Libadalang for an Ada codebase.

Run as `python lal_mcp/server.py [-P project.gpr ...]` or `python -m lal_mcp ...`.
Without -P, the project comes from a `.lal-mcp.toml` in the client's workspace
(see lal_mcp/discovery.py), so one global MCP entry works in every repository.
The project is resolved and Libadalang imported lazily on the first tools/call,
and `main()` moves fd 0/1 out of the way before anything else, so native
Libadalang/GPR code can never touch the JSON-RPC stream.
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

from lal_mcp import discovery
from lal_mcp.session import Config, Session, ToolFailure, cap, last_line
from lal_mcp.stdio_guard import protect_stdio

log = logging.getLogger("lal_mcp")

INSTRUCTIONS = """\
Ada code intelligence from Libadalang name resolution over the project's own GPR file.
VERIFY BEFORE ASSERTING: check a subprogram's signature, a type's components, or who calls
something with these tools before stating it or editing code that depends on it.
"unresolved" / "[approximate]" mean unknown: say so, never fill the gap with a guess.
Locations are path:line relative to the workspace; quote declaration text exactly as shown.
Which tool: file of a unit -> find_unit; files of a package -> package_files;
what is declared in a unit -> outline (instead of reading the file); what a name at
file:line:col is -> goto_definition; who uses it -> find_references; anything else ->
run_lal (check names with api_help, see node shapes with dump).
run_lal: Python with lal, ctx, prj, unit(name_or_path), units(), rel(node) -> "path:line";
the value of a final expression is printed. f_xxx = syntax child, p_xxx = semantic property
(may raise lal.PropertyError; some take arguments, e.g. p_referenced_decl()).
  # subprograms declared in a unit
  for d in unit("Pkg").root.findall(lal.BasicSubpDecl): print(rel(d), d.p_defining_name.text)
  # what the name at line 42, column 7 of a file refers to
  n = unit("src/x.adb").root.lookup(lal.Sloc(42, 7)); print(rel(n.p_referenced_decl()))
  # callers of Pkg.Op
  dn = unit("Pkg").root.find(lambda n: n.is_a(lal.DefiningName) and n.text == "Op")
  for r in dn.p_find_all_calls(units()): print(rel(r.ref), r.kind)
  # components of record type Pkg.Rec
  t = unit("Pkg").root.find(lambda n: n.is_a(lal.TypeDecl) and n.p_defining_name.text == "Rec")
  for c in t.findall(lal.ComponentDecl): print(rel(c), " ".join(c.text.split()))
"""

CLI = discovery.CliSettings()
SESSION: Session | None = None


async def _client_roots(ctx) -> list[str]:
    """Local paths of the client's MCP roots, if it supports roots."""
    try:
        caps = ctx.client_capabilities if ctx is not None else None
        if caps is None or caps.roots is None:
            return []
        with anyio.fail_after(5):
            res = await ctx.session.list_roots()
        return [p for r in res.roots if (p := discovery.uri_to_path(str(r.uri)))]
    except Exception as exc:  # a client that declares roots but fails to answer
        log.warning("roots/list failed: %s", last_line(exc))
        return []


async def prepare(ctx) -> None:
    """Pick the project on the first tool call. ConfigError is not cached: fixing the
    config (e.g. adding .lal-mcp.toml) takes effect on the next call."""
    global SESSION
    if SESSION is not None:
        return
    roots = [] if CLI.project or CLI.config else await _client_roots(ctx)
    r = discovery.resolve(CLI, roots, os.getcwd())
    sess = Session(Config(project=r.project, workspace=r.workspace, scenario=r.scenario, target=r.target,
                          runtime=r.runtime, max_chars=r.max_chars, timeout=r.timeout))
    if not CLI.project:
        sess.load_note = f"[project: {r.source}]\n"
    log.info("project %s, workspace %s, scenario %s", r.project, r.workspace, r.scenario)
    SESSION = sess


async def run(ctx, fn: Callable[..., tuple[str, bool]], *args: Any, hint: str = "narrow the query",
              needs_project: bool = True) -> CallToolResult:
    """Tool entry point: resolve the project if needed, then run the tool body."""
    if needs_project:
        try:
            await prepare(ctx)
        except discovery.ConfigError as exc:
            return CallToolResult(content=[TextContent(type="text", text=f"not configured: {exc}")], is_error=True)
    return call(fn, *args, hint=hint, needs_project=needs_project)


def call(fn: Callable[..., tuple[str, bool]], *args: Any, hint: str = "narrow the query",
         needs_project: bool = True) -> CallToolResult:
    """Run a tool body against the warm session; every failure becomes an isError result."""
    max_chars = SESSION.cfg.max_chars if SESSION else discovery.DEFAULT_MAX_CHARS
    try:
        if not needs_project:
            text, is_error = fn(SESSION, *args)
            return CallToolResult(content=[TextContent(type="text", text=cap(text, max_chars, hint))],
                                  is_error=is_error)
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
    register(app, run)
    return app


def parse_args(argv: list[str] | None = None) -> discovery.CliSettings:
    p = argparse.ArgumentParser(
        prog="lal-mcp", description="Libadalang MCP server (stdio)",
        epilog="Without -P/--config the project comes from .lal-mcp.toml in the client's workspace.")
    p.add_argument("-P", "--project", help="root .gpr project file (default: from .lal-mcp.toml)")
    p.add_argument("--config", help="a .lal-mcp.toml to use instead of searching for one")
    p.add_argument("-w", "--workspace", help="workspace root for relative paths")
    p.add_argument("-X", dest="scenario", action="append", default=[], metavar="VAR=VALUE",
                   help="scenario variable (repeatable; overrides the config file)")
    p.add_argument("--target", help="GPR target")
    p.add_argument("--RTS", dest="runtime", help="Ada runtime")
    p.add_argument("--max-chars", type=int, help=f"hard cap per response (default {discovery.DEFAULT_MAX_CHARS})")
    p.add_argument("--timeout", type=int, help=f"run_lal timeout in seconds (default {discovery.DEFAULT_TIMEOUT})")
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
    return discovery.CliSettings(project=a.project, config=a.config, workspace=a.workspace, scenario=scenario,
                                 target=a.target, runtime=a.runtime, max_chars=a.max_chars, timeout=a.timeout)


async def _serve(app: MCPServer, wire_in, wire_out) -> None:
    # Explicit streams: the transport uses only our private duplicates of the real pipes.
    async with stdio_server(stdin=anyio.wrap_file(wire_in), stdout=anyio.wrap_file(wire_out)) as (read_stream, write_stream):
        low = app._lowlevel_server  # same as MCPServer.run_stdio_async (mcp==2.3.0)
        await low.run(read_stream, write_stream, low.create_initialization_options())


def main(argv: list[str] | None = None) -> None:
    global CLI
    wire_in, wire_out = protect_stdio()  # first: native code must never touch the real pipes
    CLI = parse_args(argv)
    app = build_app()
    log.info("lal-mcp starting: project %s, cwd %s (resolved on first tool call)",
             CLI.project or CLI.config or "from .lal-mcp.toml", os.getcwd())
    anyio.run(_serve, app, wire_in, wire_out)


if __name__ == "__main__":
    main()
