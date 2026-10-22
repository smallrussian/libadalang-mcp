# lal-mcp — Libadalang MCP server

## What this is

A stdio MCP server that exposes Libadalang (Python API) so a coding agent can
query an Ada codebase semantically instead of reading whole files. Two goals,
equally important:

1. **Grounding / less hallucination.** The agent should get facts from
   Libadalang's name resolution (the real declaration, the real overload, the
   real callers, the real `with` closure under our GPR) instead of guessing from
   grep hits, names that look alike, or files it never opened. Every answer
   must be checkable: workspace-relative `path:line` on every fact.
2. **Token efficiency.** Every tool answers a narrow question with a short
   answer, so the context window holds the relevant facts, not whole files.

**Primary consumer: OpenAI Codex CLI talking to GPT through a custom
OpenAI-compatible proxy.** Claude Code is a secondary client used for
development/testing. Every design decision must work for Codex + GPT first.

## Hard constraints

- **Use the official `mcp` Python SDK** (`FastMCP`, stdio transport). Keep
  other dependencies to a minimum and pin them in `requirements.txt` — this may
  move to an air-gapped network later, so don't pull in anything casually.
- **Use the codebase's existing GPR project.** Never generate or require new
  `.gpr` files. Load with `lal.GPRProject(gpr, scenario_vars=..., target=...,
  runtime=...)` and `prj.create_unit_provider()`. Project path and `-X`
  scenario vars come from server CLI args.
- **Never write to the user's source tree.** Read-only tool.
- Python: our own Python 3.14 in a venv. The libadalang Python package is a
  pure-Python ctypes wrapper that loads `libadalang.so` at import time, so it
  isn't tied to GNAT Pro's interpreter. It needs `PYTHONPATH` pointing at the
  GNAT Pro libadalang Python package and the loader able to find
  `libadalang.so` and its dependencies (`LD_LIBRARY_PATH` on Linux, `PATH`
  on Windows). Verify the import first; if it fails, report the exact error.

## stdout is the protocol — protect it

- Libadalang/GPR loading is C code that can write straight to fd 1 and corrupt
  the JSON-RPC stream. At startup, dup the real stdout (`os.dup(1)`), point
  fd 1 at stderr (`os.dup2(2, 1)`), and give the SDK's stdio transport the
  saved fd (e.g. build `sys.stdout` for the transport from it before
  `mcp.run()`). Verify with a test that a C-level write during project load
  doesn't break the session.
- `run_lal` captures user-code output with `contextlib.redirect_stdout(StringIO)`.
- Log to stderr (or a file), never stdout.

## Startup / latency

- Respond to `initialize` immediately. Load the GPR project + AnalysisContext
  **lazily** on the first `tools/call` (Codex has an MCP startup timeout).
- Keep one warm `AnalysisContext` for the process lifetime. Re-parse a file
  when its mtime changes (`ctx.get_from_file(f, reparse=True)`); catch
  `lal.StaleReferenceError` and retry once after reparse.

## Tools (keep the list small — schemas are sent on every model request)

Structured, deterministic tools:

- `find_unit(name)` → spec/body paths for a unit name.
- `package_files(name, children=true, deps=false, specs_only=false)` →
  spec, body, child units, subunits; optionally the dependency closure.
  Port the logic from `ada_deps.py` (already written, see repo root).
- `outline(target)` → declarations with line numbers and subprogram specs,
  **no bodies**. `target` = unit name or file path.
- `goto_definition(file, line, column)` → decl location + decl text (capped).
- `find_references(file, line, column, limit=50)` → `path:line: <line text>`.

Open-ended tools (the model writes its own queries — see LAL docs §3, §8):

- `run_lal(code)` → exec a Python snippet with preloaded globals
  `lal, ctx, prj, unit(name_or_path), rel(path)`; returns captured stdout.
- `api_help(name)` → docstring/signature for a `lal` class, method or
  property (`inspect`/`pydoc`), e.g. `"WithClause"`, `"Name.p_referenced_decl"`.
- `dump(target, start_line, end_line, max_depth=4)` → `dump_str()` of the
  smallest node(s) covering that line range, depth-limited.

## Output rules (this is the whole point)

- Plain text, not JSON, unless the tool is explicitly structured. Paths
  relative to the workspace root, forward slashes, `path:line` form.
- Hard cap every response (default 8,000 chars, CLI-configurable). When
  truncated, say so and say how to narrow the query.
- Never return full serialized trees (`to_json`/`to_data`) — they are larger
  than the source.
- **Never return a plausible-looking guess.** If name resolution fails, the
  unit isn't in the project, or a property raised, say exactly that
  (`unresolved: Foo.Bar at src/x.adb:42`). A clear "don't know" is better
  than a wrong answer the model will build on.
- If a result came from an imprecise fallback (`imprecise_fallback=True`) or
  a partial parse (unit has diagnostics), label it `[approximate]`.
- Show the actual declaration text (capped) with definitions, not just a
  location, so the model quotes real signatures instead of inventing them.
- Errors are results, not crashes: `PropertyError`, `StaleReferenceError`,
  exceptions in `run_lal` → short message + `isError: true` so the model can
  self-correct. Include the last traceback line only.

## Tool schemas must be GPT-friendly

Codex forwards MCP `inputSchema` to the model as OpenAI function definitions.
Keep schemas boring: flat `object`, `string`/`integer`/`boolean` properties,
`required` list, short `description`s. No `oneOf`/`anyOf`/`$ref`, no nested
objects, no `format`. Every property gets a description. Tool descriptions
≤ 2 sentences; put usage guidance in the server `instructions` string returned
from `initialize`.

## run_lal safety

- `signal.alarm` timeout (default 20 s) around exec (Linux, main thread).
- Output cap as above. No file writes are expected; don't add a sandbox
  beyond that — it's a local dev tool — but never pass user code to a shell.

## Verified Libadalang Python API facts (checked against AdaCore/libadalang source)

- `lal.GPRProject(project_file, scenario_vars={}, target=None, runtime=None, ...)`;
  `.create_unit_provider(project=None)`; `.source_files(mode=lal.SourceFilesMode.default)`;
  `.errors` is a property (list of str).
- `ctx.get_from_provider(name, kind)` with `lal.AnalysisUnitKind.unit_specification`
  / `unit_body`; raises `lal.InvalidUnitNameError`.
- `unit.root` may be a `CompilationUnit` or a list of them.
- `CompilationUnit.f_prelude`, `.f_body`, `.p_syntactic_fully_qualified_name`
  (returns list of str). `WithClause.f_packages`, `.f_has_limited`, `.f_has_private`.
  `Subunit.f_name`, `.f_body`. `BodyStub.p_syntactic_fully_qualified_name`.
- Node helpers: `find`, `findall`, `finditer`, `lookup(lal.Sloc(line, col))`,
  `parents()`, `children`, `text`, `sloc_range`, `dump_str()`, `is_a(...)`.
- Semantic: `Name.p_referenced_decl()`, `DefiningName.p_find_all_references(units)`,
  `DefiningName.p_find_all_calls(units)`, `BasicDecl.p_fully_qualified_name`,
  `BasicDecl.p_defining_name`. Properties may raise `lal.PropertyError` on
  illegal code.
If a property name is uncertain, check with `help()` against the installed
version rather than guessing — GNAT Pro 26.1 may lag master.

## Layout

```
lal_mcp/
  server.py        # FastMCP app, tool registration, fd juggling, arg parsing
  session.py       # GPRProject/AnalysisContext lifecycle, reparse, rel paths
  tools/           # one module per tool group; each returns (text, is_error)
  deps.py          # logic ported from ada_deps.py
tests/
  fixture/         # tiny Ada project: .gpr, parent pkg, child pkg, separate,
                   # limited with, non-GNAT naming via Naming package
  test_tools.py    # skipped if libadalang missing
  test_protocol.py # spawns server, talks to it with the mcp client
ada_deps.py        # existing CLI, keep working
```

## Testing

- `pytest`. Protocol tests (via the SDK's client over stdio) must run even
  without libadalang: initialize, tools/list, error paths.
- `scripts/smoke.py`: launch the server, call every tool on the fixture,
  print response sizes in chars — watch these numbers.

## Git: commit and push as you go

- The repo is already initialized with a remote and a `.gitignore`. Don't
  re-init, don't change the remote, don't rewrite `.gitignore` wholesale
  (append if something new needs ignoring).
- **Commit after every self-contained, working change** — a new tool with its
  test, a fixture, a fix. Small commits; never one giant commit per step.
  Run the tests before committing; don't commit red unless the message says
  `wip:` and explains why.
- **Push immediately after every commit**: `git push` (first push:
  `git push -u origin HEAD`). Never let local commits pile up.
- Conventional commit messages: `feat(tools): add outline tool`,
  `test: add fixture with Naming package`, `fix(session): reparse on mtime change`,
  `docs: codex config in README`. Subject ≤ 72 chars; body says *why* if not obvious.
- Stage specific paths (`git add lal_mcp/tools/outline.py tests/...`), not
  `git add -A`. Check `git status` before each commit so nothing stray
  (fixture build outputs, caches, `.env`) gets in.
- Never `--force` push, `reset --hard`, rebase published commits, or amend a
  commit that's already pushed. If a push is rejected or fails, stop and tell
  me — don't try to "fix" history.
- Work on the current branch unless told otherwise.

## Codex wiring (document in README, don't hardcode)

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
command = "/path/to/lal_mcp/.venv/bin/python"   # our Python 3.14
args = ["/path/to/lal_mcp/server.py", "-P", "/ws/mission.gpr", "-w", "/ws", "-XBUILD=debug"]
env = { PYTHONPATH = "...", LD_LIBRARY_PATH = "..." }   # Codex does not pass the full shell env
startup_timeout_sec = 30
tool_timeout_sec = 120
```

Also ship an `AGENTS.md` snippet for Ada repos telling Codex to:

- prefer `outline` / `package_files` / `goto_definition` over reading whole files;
- **verify before asserting**: check a subprogram's signature, a type's
  components, or who calls something with the tools before stating it or
  editing code that depends on it;
- treat `unresolved` / `[approximate]` as unknowns to say out loud, not to fill in;
- never create `.gpr` files.
The server `instructions` string should carry the same verify-first rule.
