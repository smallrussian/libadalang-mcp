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
import time
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
"unresolved" / "[approximate]" / "[status: INCOMPLETE" mean unknown: stop and report it,
never fill the gap with a guess or by reading whole files.
Locations are path:line relative to the workspace; quote declaration text exactly as shown.
Which tool: file of a unit -> find_unit; files of a package -> package_files;
what is declared in a unit -> outline (instead of reading the file); what a name at
file:line:col is -> goto_definition; who uses it -> find_references; what a unit needs to
link (Import'ed C symbols, owning GPR projects, linker options, missing bodies) -> link_info;
facts for testing a subprogram (ranges, boundaries, branches, raises, state, doubles) ->
test_model; anything else -> run_lal (check names with api_help, node shapes with dump).
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
                          runtime=r.runtime, max_chars=r.max_chars, timeout=r.timeout,
                          tests_project=r.tests_project, gnattest=r.gnattest))
    if not CLI.project:
        sess.load_note = f"[project: {r.source}]\n"
    log.info("project %s, workspace %s, scenario %s", r.project, r.workspace, r.scenario)
    SESSION = sess


async def run(ctx, fn: Callable[..., tuple[str, bool]], *args: Any, hint: str = "narrow the query",
              needs_project: bool = True) -> CallToolResult:
    """Tool entry point: resolve the project if needed, then run the tool body. Logs one line per call."""
    t0 = time.monotonic()
    if needs_project:
        try:
            await prepare(ctx)
        except discovery.ConfigError as exc:
            result = CallToolResult(content=[TextContent(type="text", text=f"not configured: {exc}")], is_error=True)
            _log_call(fn, args, result, t0)
            return result
    result = call(fn, *args, hint=hint, needs_project=needs_project)
    _log_call(fn, args, result, t0)
    return result


def _log_call(fn, args: tuple, result: CallToolResult, t0: float) -> None:
    text = "".join(getattr(c, "text", "") for c in result.content)
    shown = ", ".join(repr(a) if len(repr(a)) <= 80 else repr(a)[:77] + "..." for a in args)
    log.info("tool %s(%s) -> %d chars in %.2fs%s", fn.__name__, shown, len(text), time.monotonic() - t0,
             " [isError]" if result.is_error else "")
    if log.isEnabledFor(logging.DEBUG):
        log.debug("tool %s response:\n%s", fn.__name__, text[:2000] + ("\n..." if len(text) > 2000 else ""))


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
        try:  # files added/removed or a .gpr edited since the last call: rebuild the project view
            refreshed = SESSION.refresh_if_changed()
        except ToolFailure as exc:
            refreshed = f"[{exc}]"
        if refreshed:
            SESSION.load_note = (SESSION.load_note or "") + refreshed + "\n"
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


def parse_args(argv: list[str] | None = None) -> tuple[discovery.CliSettings, argparse.Namespace]:
    p = argparse.ArgumentParser(
        prog="lal-mcp", description="Libadalang MCP server (stdio, or streamable HTTP with --http)",
        epilog="Without -P/--config the project comes from .lal-mcp.toml: in stdio mode searched from the "
               "client's workspace, in --http mode from the directory the server is started in.")
    p.add_argument("-P", "--project", help="root .gpr project file (default: from .lal-mcp.toml)")
    p.add_argument("--config", help="a .lal-mcp.toml to use instead of searching for one")
    p.add_argument("-w", "--workspace", help="workspace root for relative paths")
    p.add_argument("-X", dest="scenario", action="append", default=[], metavar="VAR=VALUE",
                   help="scenario variable (repeatable; overrides the config file)")
    p.add_argument("--target", help="GPR target")
    p.add_argument("--RTS", dest="runtime", help="Ada runtime")
    p.add_argument("--max-chars", type=int, help=f"hard cap per response (default {discovery.DEFAULT_MAX_CHARS})")
    p.add_argument("--tests-project", metavar="HARNESS",
                   help="GNATtest harness to load instead of the project: test_driver.gpr, test_drivers.gpr "
                        "(--separate-drivers) or their directory (default: auto-detected in "
                        "<object dir>/gnattest/harness)")
    p.add_argument("--no-gnattest", action="store_true", help="don't auto-detect a GNATtest harness")
    p.add_argument("--timeout", type=int,
                   help=f"run_lal timeout in seconds, 0 = none, e.g. under a debugger (default {discovery.DEFAULT_TIMEOUT})")
    p.add_argument("--log-level", default="INFO", type=str.upper,
                   choices=["TRACE", "DEBUG", "INFO", "WARNING", "ERROR"],
                   help="INFO (default): one line per tool call; DEBUG: plus each response; "
                        "TRACE: plus the MCP SDK's own protocol logging")
    p.add_argument("--log-file", help="also write the log to this file (appended), e.g. to watch it")
    h = p.add_argument_group("HTTP mode (the client connects by URL instead of launching the server)")
    h.add_argument("--http", action="store_true", help="serve streamable HTTP at http://HOST:PORT/mcp")
    h.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1)")
    h.add_argument("--port", type=int, default=8765, help="port (default 8765)")
    h.add_argument("--token-env", metavar="NAME",
                   help="require 'Authorization: Bearer <token>', token read from env var NAME; "
                        "mandatory when --host is not a loopback address")
    a = p.parse_args(argv)
    if a.http:
        from lal_mcp.http_server import is_loopback

        if a.token_env and not os.environ.get(a.token_env):
            p.error(f"--token-env {a.token_env}: environment variable is not set or empty")
        if not is_loopback(a.host) and not a.token_env:
            p.error(f"--host {a.host} is reachable from other machines and run_lal executes Python; "
                    "add --token-env NAME (or bind 127.0.0.1)")
    scenario: dict[str, str] = {}
    for item in a.scenario:
        if "=" not in item:
            p.error(f"-X expects VAR=VALUE, got {item!r}")
        k, v = item.split("=", 1)
        scenario[k] = v
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if a.log_file:
        handlers.append(logging.FileHandler(a.log_file, encoding="utf-8"))
    level = a.log_level.upper()
    # DEBUG: lal-mcp's own details (each tool response), libraries at INFO.
    # TRACE: everything, including the MCP SDK's per-message/SSE-chunk logging.
    logging.basicConfig(level="DEBUG" if level == "TRACE" else ("INFO" if level == "DEBUG" else level),
                        handlers=handlers, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    if level == "DEBUG":
        logging.getLogger("lal_mcp").setLevel(logging.DEBUG)
    cli = discovery.CliSettings(project=a.project, config=a.config, workspace=a.workspace, scenario=scenario,
                                target=a.target, runtime=a.runtime, max_chars=a.max_chars, timeout=a.timeout,
                                tests_project=a.tests_project, gnattest=False if a.no_gnattest else None)
    return cli, a


async def _serve(app: MCPServer, wire_in, wire_out) -> None:
    # Explicit streams: the transport uses only our private duplicates of the real pipes.
    async with stdio_server(stdin=anyio.wrap_file(wire_in), stdout=anyio.wrap_file(wire_out)) as (read_stream, write_stream):
        low = app._lowlevel_server  # same as MCPServer.run_stdio_async (mcp==2.3.0)
        await low.run(read_stream, write_stream, low.create_initialization_options())


def main(argv: list[str] | None = None) -> None:
    global CLI, SESSION
    wire_in, wire_out = protect_stdio()  # first: native code must never touch the real pipes
    CLI, opts = parse_args(argv)
    app = build_app()
    if not opts.http:
        log.info("lal-mcp starting (stdio): project %s, cwd %s (resolved on first tool call)",
                 CLI.project or CLI.config or "from .lal-mcp.toml", os.getcwd())
        anyio.run(_serve, app, wire_in, wire_out)
        return

    # HTTP mode: one long-lived server per project, so resolve and load it up front
    # (no client roots to search from, and no client startup timeout to respect).
    from lal_mcp.http_server import serve

    try:
        r = discovery.resolve(CLI, [], os.getcwd())
    except discovery.ConfigError as exc:
        log.error("not configured: %s", exc)
        sys.exit(2)
    SESSION = Session(Config(project=r.project, workspace=r.workspace, scenario=r.scenario, target=r.target,
                             runtime=r.runtime, max_chars=r.max_chars, timeout=r.timeout,
                          tests_project=r.tests_project, gnattest=r.gnattest))
    log.info("project: %s", r.source)
    try:
        SESSION.ensure_loaded()  # fail fast and make the first query quick
    except ToolFailure as exc:
        log.error("%s", exc)  # e.g. libadalang import failed; keep serving so tool calls report it
    token = os.environ.get(opts.token_env) if opts.token_env else None
    anyio.run(serve, app, opts.host, opts.port, token, opts.log_level)


if __name__ == "__main__":
    main()
