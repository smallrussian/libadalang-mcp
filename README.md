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
| `link_info(name, deps)` | build/link closure: `pragma Import`/`Export` symbols and the C/C++ source or library defining them, owning GPR projects (`with`s needed), linker options, missing bodies |
| `test_model(file, line, column)` | facts for testing a subprogram: parameter ranges and boundary values (modular width), contracts, branches, raises, returns, global state, callees to double, bit operations |
| `reload_project()` | rebuild the project view now (normally automatic, see below) |
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
| `--log-level` | `INFO` (default): one line per tool call; `DEBUG`: plus each response; `TRACE`: plus the MCP SDK's protocol logging |
| `--log-file` | also write the log to this file, e.g. to watch it (see [Debugging](#debugging)) |
| `--http`, `--host`, `--port`, `--token-env` | serve over HTTP instead of stdio; see [HTTP mode](#http-mode-the-client-connects-instead-of-launching) |

`initialize` is answered immediately. The project is chosen and loaded on the
first tool call, then kept warm. Logs go to stderr; stdout carries only the protocol.

### Project changes

The server keeps a long-lived view of the project: source list, unit lookup,
caches. That matters most in HTTP mode, where it runs for days.

- **Edited files** are re-parsed when their modification time changes.
- **Added, removed or re-added files, and `.gpr` edits,** are checked before
  each tool call, at most once a second. The check is cheap: it stats the
  project's `.gpr` files and source directories. When something changed, the
  project is reloaded, all derived caches are dropped, and the answer starts
  with e.g. `[project reloaded: 1 file(s) added in src; 13 sources (+1 / -0)]`.
  Editors that save by writing a temp file and renaming it don't trigger a
  reload: only a change in the set of Ada files in a directory does.
- **A broken `.gpr`** (say, mid-edit) doesn't take the server down. It keeps
  answering from the previous view, says so, and retries on the next call.
- **`reload_project`** forces a reload. Use it where directory timestamps aren't
  reliable (some network drives), or after changing environment variables a
  project reads.

### GNATtest (GNATdas) test harness

For the full workflow and best practices (layouts, where tests should live,
writing, building and running tests, troubleshooting), see
[docs/gnattest.md](docs/gnattest.md).

[GNATtest](https://docs.adacore.com/R/docs/gnat-27.0/gnatdas/html/gnatdas_ug/gnattest/gnattest_part.html)
generates a test skeleton per subprogram and a harness project. Both live in the
project's object directory, outside its sources:

```bash
gnattest -P app.gpr bit_ops.ads          # skeletons for one unit (omit the file for all)
gprbuild -P obj/gnattest/harness/test_driver.gpr
obj/gnattest/harness/test_runner         # "Test not implemented." until each body is written
```

With `--separate-drivers`, each unit (`=unit`) or each test (`=test`) gets its
own driver and runner. gnattest then writes the aggregate project
`test_drivers.gpr` instead of `test_driver.gpr`. In gnattest 26.1 the switch only
takes effect **without** source file arguments:

```bash
gnattest -P app.gpr --separate-drivers=unit
gprbuild -P obj/gnattest/harness/test_drivers.gpr        # all drivers
obj/gnattest/harness/Bit_Ops.Test_Data.Tests/bit_ops-test_data-tests-suite-test_runner
```

The server looks in `<object dir>/gnattest/harness/` (or `Gnattest'Harness_Dir`)
for `test_drivers.gpr` and `test_driver.gpr`, and loads the one it finds as the
root project. If both exist, it loads the newer one. Since the harness withs your
project, every answer about your code stays the same, and the generated test
packages become sources too:

- `test_model` shows a subprogram's generated test, and whether it's still a
  skeleton, e.g.
  `gnattest test: app/obj/gnattest/tests/bit_ops-test_data-tests.adb:36 Test_Set_Bit (generated skeleton, ...)`.
- With separate drivers, `test_model` also names the driver and runner that
  run that test. Each driver registers its tests by spec position
  (`"bit_ops.ads:14:4:"`), so the match is exact, even for `=test` drivers.
- A harness generated or regenerated while the server runs is picked up before
  the next call: `[project reloaded: test_drivers.gpr created; ..., GNATtest
  harness ... (separate drivers: 3)]`. Each driver project is watched too.
- `reload_project` reports the loaded harness and lists its test packages. With
  separate drivers, it also lists each driver: its units, its runner, and
  whether the runner is built yet.
- If the harness doesn't load, for example a stale one, the server uses the
  project alone and says why.
- Settings: `tests_project = "..."` in `.lal-mcp.toml` (or `--tests-project`)
  points at a harness file or harness directory elsewhere. Use it when you pass
  `--harness-dir` to gnattest. `gnattest = false` (or `--no-gnattest`) turns
  detection off.

Where things go:

- **Test bodies** (`*-test_data-tests.adb`) and **`Test_Data`** packages are
  your code. gnattest keeps them when it regenerates. By default they go to each
  project's `obj/gnattest/tests/`, but `obj/` is usually git-ignored and wiped by
  cleans. Put them in the source tree with `--tests-root=<repo>/test/gnattest`
  (tests mirror each source directory), and commit them. The server finds them
  through the harness, wherever they are.
- **The harness** (everything under `obj/gnattest/harness/`) is regenerated on
  every run. Leave it in `obj/`, and don't edit or commit it.
- Pass the same switches (mode, `--tests-root`) on every regeneration.
  Otherwise gnattest starts a fresh set of skeletons somewhere else.
- `gprbuild -P test_drivers.gpr` stops at the first unit that doesn't compile.
  In that case, build one driver's own project.
- `gnattest test_drivers.list`, which is meant to run every driver, ran nothing
  in our 26.1 test (`no test driver in source table`). Run the runners
  directly.

The harness location comes from `gprinspect`, so detection needs GNAT Pro's
`bin` on the server's `PATH`.

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
  of your Ada repositories. Codex doesn't act on the server's own instructions:
  without this guidance, it ignored the tools in testing. The snippet adds the
  verify-before-asserting rule, the uncertainty gate (stop on
  `[status: INCOMPLETE ...]` / `unresolved` instead of guessing), the build/link
  rules, and the one-repair failure loop.
- For test work, copy the skills in [`docs/agents/skills/`](docs/agents/skills/)
  to the repository's `.agents/skills/` (or `~/.agents/skills/` for all repos):
  - `ada-unit-tests` writes tests: link closure first, then a semantic test
    matrix, then independently derived expected values;
  - `ada-test-runner` prepares and runs tests, and never edits authored tests.

  Codex picks a skill by its description, or you name it explicitly with `$ada-unit-tests`.
- `link_info` and `test_model` begin with `[status: complete]` or
  `[status: INCOMPLETE: ...]`, a machine-checkable gate for the workflow.
  `link_info` uses `gprinspect` and `nm` from GNAT Pro's `bin`, which must be on
  the server's `PATH` (it already is for the runtime).
- If Codex's sandbox won't start the server ("Access is denied (os error 5)" on
  Windows), use HTTP mode below. Then Codex connects to a server you started
  yourself, instead of launching Python inside its sandbox. The sandbox can stay
  restricted (`read-only`, Windows `unelevated`); full access isn't needed.

### Approvals: why tool calls can seem to hang

Codex asks before running an MCP tool unless the tool declares itself
read-only. All lal-mcp tools do (MCP `readOnlyHint`), except `run_lal`: it
executes Python, so it's marked as possibly destructive, and Codex asks before
each `run_lal` call. If your client doesn't surface Codex's approval prompt (a
custom VS Code extension, for example), the call just waits, and looks like a
stall. In `codex exec` with `approval_policy = "never"` it fails instead, with
"MCP tool call requires approval".

To run `run_lal` without asking too, add this to the server's entry:

```toml
[mcp_servers.ada]
default_tools_approval_mode = "approve"   # never ask for this server's tools
```

Verified with codex-cli 0.163. `"auto"` is *not* enough: it follows the tools'
annotations, so `run_lal` still asks.

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
# default_tools_approval_mode = "approve"   # also run run_lal without asking (see Approvals above)
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

## Debugging

Watch what the server does: one log line per tool call, with arguments, time,
response size and errors.

```powershell
python lal_mcp\server.py --http --port 8765 --log-level DEBUG --log-file lal-mcp.log
Get-Content lal-mcp.log -Wait          # in another terminal (Linux: tail -f lal-mcp.log)
```

```
... lal_mcp INFO tool goto_definition('core/src/gnatcoll-json.adb', 846, 19) -> 256 chars in 0.05s
... lal_mcp INFO tool run_lal('print(len(units()))') -> 4 chars in 0.11s
```

`DEBUG` adds each response's text; `TRACE` adds the MCP SDK's own protocol
logging (every message, very verbose). If the log shows the client connecting
(`POST /mcp`, `GET /mcp`) but no `tool ...` lines, the client never sent the
call. With Codex that usually means an approval prompt is waiting (see
[Approvals](#approvals-why-tool-calls-can-seem-to-hang)).

**VS Code debugger:** [`.vscode/launch.json`](.vscode/launch.json) has three
configurations:

- the HTTP server on the test fixture;
- on an Ada repository (its `.lal-mcp.toml`);
- on an explicit `.gpr`.

Pick the Python that can import libadalang (*Python: Select Interpreter*),
start a configuration, point Codex or Claude at `http://127.0.0.1:<port>/mcp`,
and breakpoints in `lal_mcp/` are hit when the client calls a tool. The
configurations use `--timeout 0`, because `run_lal`'s time limit on Windows
uses the same trace hook as the debugger. They log to `lal-mcp-debug.log` in
this folder.

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
