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
|---|---|
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

## Requirements

- Python 3.10+ (tested with 3.12 on Linux and 3.14 on Windows).
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
`LD_LIBRARY_PATH` isn't needed for it. Pick one of these two options.

**Option A: pip-install the wheel** into the Python that runs the server:

```bash
python -m pip install <prefix>/share/libadalang/python/libadalang-*.whl
```

**Option B: no pip (e.g. air-gapped).** A wheel is a zip file. Unpack it once
and point `PYTHONPATH` at the directory:

```bash
python -c "import zipfile,glob,sys; zipfile.ZipFile(glob.glob(sys.argv[1])[0]).extractall(sys.argv[2])" \
    "<prefix>/share/libadalang/python/libadalang-*.whl" /opt/lal-python
export PYTHONPATH=/opt/lal-python
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
env -i HOME="$HOME" PATH="<prefix>/bin:/usr/bin:/bin" PYTHONPATH="<only for option B>" \
  /path/to/python -c "
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

## Running the server

```
python /path/to/lal-mcp/lal_mcp/server.py -P /ws/mission.gpr -w /ws -XBUILD=debug
```

| argument | meaning |
|---|---|
| `-P`, `--project` | root `.gpr` file (required). Your existing project is used as is. |
| `-w`, `--workspace` | root for relative paths in answers (default: the project's directory) |
| `-X VAR=VALUE` | scenario variable, repeatable (`-XBUILD=debug` also works) |
| `--target`, `--RTS` | GPR target and runtime, if the project needs them |
| `--max-chars` | hard cap per response (default 8000) |
| `--timeout` | `run_lal` time limit in seconds (default 20) |
| `--log-level` | log level for stderr (default INFO) |

`initialize` is answered immediately. The project is loaded on the first tool
call and kept warm. Files are re-parsed when their mtime changes. Logs go to
stderr; stdout carries only the protocol.

## Codex configuration

Codex starts MCP servers with a reduced environment, not your full shell
environment, so put `PATH` (and `PYTHONPATH` for option B) in `env`.

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
args = ["/path/to/lal-mcp/lal_mcp/server.py", "-P", "/ws/mission.gpr", "-w", "/ws", "-XBUILD=debug"]
env = { PATH = "/opt/gnatpro/26.1/bin:/usr/bin:/bin" }
# option B instead of pip:  env = { PATH = "...", PYTHONPATH = "/opt/lal-python" }
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

For Claude Code, the equivalent is:

```bash
claude mcp add ada -e PATH=/opt/gnatpro/26.1/bin:/usr/bin:/bin -- /path/to/python /path/to/lal-mcp/lal_mcp/server.py -P /ws/mission.gpr -w /ws
```

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
