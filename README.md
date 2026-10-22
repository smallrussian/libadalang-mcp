# lal-mcp — Libadalang MCP server

A stdio [MCP](https://modelcontextprotocol.io) server that lets a coding agent
query an Ada codebase through [Libadalang](https://github.com/AdaCore/libadalang)
name resolution, using the project's own GPR file, instead of grepping and
reading whole files.

- **Grounded answers.** Definitions, overloads, references and dependency
  closures come from Libadalang, not from names that look alike. Every fact
  carries a workspace-relative `path:line`. When something can't be resolved,
  the tool says `unresolved: ...`. Results from an imprecise fallback, or from a
  file with parse errors, are labeled `[approximate]`.
- **Small answers.** Each tool answers one narrow question in plain text, with
  a hard cap per response (default 8,000 chars).
- **Read-only.** It never writes to your source tree and never generates `.gpr`
  files.

Primary client: OpenAI Codex CLI. Works with any MCP client (Claude Code etc.).

## Tools

| tool | answers |
| --- | --- |
| `find_unit(name)` | spec/body files of a unit (GPR naming scheme aware) |
| `package_files(name, children, deps, specs_only)` | spec, body, `separate` subunits, child units, optional dependency closure |
| `outline(target)` | declarations with line numbers and signatures, no bodies |
| `goto_definition(file, line, column)` | the declaration a name resolves to, its text, its other parts (body, stub) |
| `find_references(file, line, column, limit)` | `path:line: source line` for every reference in the project |
| `run_lal(code)` | runs a Python snippet with `lal`, `ctx`, `prj`, `unit()`, `units()`, `rel()` preloaded |
| `api_help(name)` | signature and docs of a Libadalang class or property |
| `dump(target, start_line, end_line, max_depth)` | syntax tree (node kinds, fields) of a line range |

The server's `instructions` (sent at `initialize`) carry a short cheat sheet and
the verify-before-asserting rule.

## How well it works

On [gnatcoll-core](https://github.com/AdaCore/gnatcoll-core), checked against
the GNAT compiler's own cross-references:

- `goto_definition` resolves 99.9% of 43,842 references to the same entity GNAT does.
- `find_references` has 100% recall and 99.9% precision.

Answers are 80–800× smaller than the files they replace. Details, a Claude A/B
comparison with and without the tools, and how to measure your own codebase:
[`docs/metrics.md`](docs/metrics.md).

## Requirements

- Python 3.11+ (tested with 3.12 on Linux and 3.14 on Windows).
- `pip install -r requirements.txt` (`mcp==2.3.0`, plus `pytest` for the tests).
- GNAT Pro and Libadalang from AdaCore, same release (tested with 26.1). Use the
  **x86_64** packages on 64-bit Linux. The `x86-linux` packages are 32-bit and
  can't be loaded by a 64-bit Python.

## Installing Libadalang for Python

Libadalang isn't on PyPI. The GNAT Pro Libadalang package ships a Python wheel:

```
<prefix>/share/libadalang/python/libadalang-<version>-py3-none-<platform>.whl
```

- `<prefix>` on Linux is wherever you ran `doinstall`, e.g. `/opt/gnatpro/26.1`.
- `<prefix>` on Windows is the installer's directory, e.g. `C:\GNATPRO\26.1`.

The wheel is **self-contained**: it bundles `libadalang.so` / `libadalang.dll`
and every library it depends on (GNAT runtime, gnatcoll, gpr2, xmlada). So
`LD_LIBRARY_PATH` isn't needed for it. Install it into the Python that runs the server:

```bash
python -m pip install <prefix>/share/libadalang/python/libadalang-*.whl
```

### PATH must contain GNAT's `bin` directory

GPR uses `PATH` to find the GNAT toolchain, and with it the Ada runtime sources.
Without it, the project still loads, but runtime units such as `Ada.Text_IO`
don't resolve. The first tool response then starts with a project warning
containing `can't find a toolchain`.

The server sets a fallback `PATH` if it's missing entirely: on Linux, an unset
`PATH` makes GPR's native code abort the whole process. That fallback doesn't
include GNAT, so set `PATH` explicitly.

### Check the setup

Run this with exactly the environment you'll give the MCP client:

```bash
env -i HOME="$HOME" PATH="<prefix>/bin:/usr/bin:/bin" /path/to/python -c "
import libadalang as lal
p = lal.GPRProject('/ws/mission.gpr', print_errors=False)
c = p.create_context()
u = c.get_from_provider('Ada.Text_IO', lal.AnalysisUnitKind.unit_specification)
print('libadalang', lal.version, '| project errors:', p.errors, '| Ada.Text_IO:', u.filename)"
```

Expect an empty error list and a path under `<prefix>` for `Ada.Text_IO`. If the
import fails, the message says which library or module is missing.

To find the paths on a machine you don't know:

```bash
dirname "$(dirname "$(command -v gnat)")"            # <prefix>, if gnat is on your PATH
ls "<prefix>"/share/libadalang/python/               # the wheel
python -c "import libadalang; print(libadalang.__file__)"   # where Python finds it
```

## Project settings: `.lal-mcp.toml`

Each Ada repository says which GPR project to load in a small file at its
root, committed with the code. The MCP client configuration then has no
project-specific arguments, and one global entry works in every repository.

```toml
# .lal-mcp.toml (paths are relative to this file)
project = "build/mission.gpr"   # required: your existing root project, used as is
# workspace = "."               # root for relative paths in answers (default: this file's directory)
# target = "x86_64-linux"       # GPR target / runtime, if the project needs them
# runtime = "light"
# max_chars = 8000              # per-response cap
# timeout = 20                  # run_lal seconds

[scenario]                      # -X scenario variables
BUILD = "debug"
```

A commented copy is in [`docs/lal-mcp.example.toml`](docs/lal-mcp.example.toml).

On its first tool call, the server picks the project in this order:

1. `-P` / `--config` given to the server (they still work, and override everything);
2. the nearest `.lal-mcp.toml`, searching upward from the client's workspace;
3. the only `.gpr` file at the workspace root, if there is exactly one,
   with GPR default scenario values. With several `.gpr` files or none, the tool
   returns an error listing what it found.

The "client's workspace" is the folder the client reports through MCP roots.
If the client reports none, it's the directory the server was started in. So
for clients without roots support, start them from inside the repository.

The first answer begins with a line like
`[project: mission.gpr from /ws/.lal-mcp.toml (found from ...)]`, so you can
see which project was picked. A missing or broken config file is an error
result, not a crash, and fixing the file takes effect on the next call, without
a restart. `-X`, `--target`, `--RTS`, `--max-chars` and `--timeout` on the
command line override the file.

## Running the server

```
python /path/to/lal-mcp/lal_mcp/server.py            # project from .lal-mcp.toml
python /path/to/lal-mcp/lal_mcp/server.py -P /ws/mission.gpr -w /ws -XBUILD=debug   # or explicit
```

| argument | meaning |
| --- | --- |
| `-P`, `--project` | root `.gpr` file. Default: from `.lal-mcp.toml` |
| `--config` | a specific `.lal-mcp.toml` to use instead of searching |
| `-w`, `--workspace` | root for relative paths in answers |
| `-X VAR=VALUE` | scenario variable, repeatable (`-XBUILD=debug` also works) |
| `--target`, `--RTS` | GPR target and runtime, if the project needs them |
| `--max-chars` | hard cap per response (default 8000) |
| `--timeout` | `run_lal` time limit in seconds (default 20) |
| `--log-level` | log level for stderr (default INFO) |
| `--http`, `--host`, `--port`, `--token-env` | serve over HTTP instead of stdio; see [HTTP mode](#http-mode-the-client-connects-instead-of-launching) |

`initialize` is answered immediately. The project is chosen and loaded on the
first tool call, then kept warm. Files are re-parsed when their mtime changes.
Logs go to stderr; stdout carries only the protocol.

## Codex configuration

One global entry for all repositories. The project comes from each repo's
`.lal-mcp.toml`, so run `codex` from inside the repository. Codex starts MCP
servers with a reduced environment, not your full shell environment, so put
`PATH` in `env`.

```toml
# ~/.codex/config.toml
model_provider = "proxy"
model = "<gpt model name the proxy exposes>"

[model_providers.proxy]
name = "internal proxy"
base_url = "https://<proxy>/v1"
env_key = "PROXY_API_KEY"
wire_api = "responses"   # or "chat" — whatever the proxy supports

[mcp_servers.ada]
command = "/path/to/python"            # the Python that can import libadalang
args = ["/path/to/lal-mcp/lal_mcp/server.py"]   # project: from the repo's .lal-mcp.toml
env = { PATH = "/opt/gnatpro/26.1/bin:/usr/bin:/bin" }
startup_timeout_sec = 30
tool_timeout_sec = 120
```

On Windows, use Windows paths, and `;` as the `PATH` separator, e.g.
`env = { PATH = "C:\\GNATPRO\\26.1\\bin;C:\\Windows\\system32" }`.

Notes:

- `tool_timeout_sec`: on a large codebase, the first call parses every source,
  and `find_references` loads every unit. Allow well over the `--timeout`
  you give `run_lal`.
- Copy [`docs/AGENTS-snippet.md`](docs/AGENTS-snippet.md) into the `AGENTS.md`
  of your Ada repositories so Codex prefers these tools and verifies before
  asserting.
- If Codex's sandbox won't start the server ("Access is denied (os error 5)" on
  Windows), use HTTP mode below. Then Codex connects to a server you started
  yourself, instead of launching Python inside its sandbox.

## HTTP mode (the client connects instead of launching)

By default the client launches the server and talks over stdin/stdout. With
`--http`, you start the server yourself, as a normal process under your own
account, and the client connects to it by URL. Use it when the client's sandbox
can't launch Python, or when you want the project loaded once and kept warm
across sessions.

Start one server per repository, from the repository root, which picks up its
`.lal-mcp.toml`. Or pass `-P` / `-X` as usual:

```bash
cd /ws && PATH=/opt/gnatpro/26.1/bin:$PATH python /path/to/lal-mcp/lal_mcp/server.py --http --port 8765
```

```powershell
cd C:\src\mission; $env:PATH = "C:\GNATPRO\26.1\bin;$env:PATH"; python C:\path\to\lal-mcp\lal_mcp\server.py --http --port 8765
```

The project is resolved and loaded at startup. A missing or broken config stops
the server with a message. Leave the terminal open; logs go there.

Codex:

```toml
[mcp_servers.ada]
url = "http://127.0.0.1:8765/mcp"
tool_timeout_sec = 120
```

Claude Code:

```bash
claude mcp add --transport http ada http://127.0.0.1:8765/mcp
```

Safety: `run_lal` executes Python in the server, so treat the port like a shell.

- The default `--host 127.0.0.1` accepts local connections only. The MCP SDK
  also rejects requests whose Host/Origin header isn't localhost (DNS-rebinding
  protection).
- `--token-env NAME` makes every request carry `Authorization: Bearer <token>`,
  with the token read from environment variable `NAME`. Clients send it with
  `bearer_token_env_var = "NAME"` (Codex) or `-H "Authorization: Bearer ..."`
  (Claude Code).
- Binding to anything other than a loopback address (e.g. `--host 0.0.0.0`) is
  refused unless `--token-env` is given.

| HTTP option | meaning |
| --- | --- |
| `--http` | serve streamable HTTP at `http://HOST:PORT/mcp` |
| `--host` | bind address (default `127.0.0.1`) |
| `--port` | port (default `8765`) |
| `--token-env NAME` | require a bearer token, read from env var `NAME` |

## Claude

For Claude Code, the equivalent is one user-level entry for all repositories,
with the project again coming from each repo's `.lal-mcp.toml`:

```bash
claude mcp add --scope user ada -e PATH=/opt/gnatpro/26.1/bin:/usr/bin:/bin -- /path/to/python /path/to/lal-mcp/lal_mcp/server.py
```

[`docs/using-with-claude.md`](docs/using-with-claude.md) covers a shared
project `.mcp.json`, Claude Desktop, permissions and headless use.

## Development

```bash
python -m pytest            # protocol tests run without libadalang; the rest skip if it's missing
python scripts/smoke.py     # every tool on tests/fixture, response size vs size of the files it replaces
python scripts/smoke.py --schemas            # tools/list exactly as sent to the client
python scripts/smoke.py --gnatcoll <path>    # same on a gnatcoll-core checkout
```

`tests/fixture/` is a small Ada project that uses a GPR `Naming` package (non-default
file names). It has a parent and a child package, an `is separate` body, a
`limited with`, a body-only main, a unit with an unresolvable name, and a unit
with a syntax error.

Layout: `lal_mcp/server.py` (MCP app, CLI, stdio protection),
`lal_mcp/session.py` (project/context lifecycle), `lal_mcp/deps.py` (unit
index, subunits, dependency closure), `lal_mcp/tools/` (one module per tool group).
