# Measuring lal-mcp with Codex

`scripts/codex_ab.py` measures whether Codex answers Ada questions better with
the `ada` tools than without them. It's the same method as the Claude numbers in
[metrics.md](metrics.md), so the results are comparable. It runs on **Windows
and Linux**.

Each question is asked twice through `codex exec`:

- **with tools:** the `ada` MCP server, plus Codex's normal read-only shell, plus
  the [AGENTS.md guidance](AGENTS-snippet.md) you'd ship with the tools. Codex
  doesn't act on an MCP server's built-in instructions by itself: without the
  guidance it ignored the tools and grepped (measured: 0 tool calls in 3 runs).
- **no tools:** the read-only shell only.

Both use the same model and sandbox (`-s read-only`, approvals off). lal-mcp
runs in [HTTP mode](../README.md#http-mode-the-client-connects-instead-of-launching),
so Codex never launches Python itself, as in interactive use. Answers are
scored against the GNAT compiler's own cross-references: every expected
`file:line` must be present, with no wrong locations.

## Run it

1. Copy [`scripts/codex_eval.example.toml`](../scripts/codex_eval.example.toml),
   e.g. to `my_codex_eval.toml`. With a ChatGPT login (`codex login`) and
   Windows' non-admin sandbox, the example works as is. For your API proxy,
   fill in the `[model_providers]` part.
2. Run:

```powershell
# Windows (Codex installed with npm: --codex can point at its codex.ps1)
python scripts\codex_ab.py --codex-config my_codex_eval.toml --codex C:\nvm4w\nodejs\codex.ps1 --gnatpro C:\GNATPRO\26.1
```

```bash
# Linux
python scripts/codex_ab.py --codex-config my_codex_eval.toml --gnatpro /opt/gnatpro/26.1
```

What it does, all under `--work` (default: your temp folder, `lal-mcp-codex-eval`):

1. Fetches gnatcoll-core at the commit `metrics.md` was measured on (one commit),
   or uses your project (`-P` / `-w` / `-X`).
2. Compiles with `gprbuild -gnatc` into a scratch build tree, for GNAT's
   cross-references. Your sources aren't written to.
3. Prints resolution accuracy against GNAT. No LLM is involved; skip it with `--skip-xref`.
4. Samples questions from GNAT's data with a fixed seed:
   - which overload a call resolves to;
   - every call site of one particular overload.

   With gnatcoll-core, the 5 hand-written questions are added too.
5. Starts lal-mcp `--http` on a free port (log: `lal-mcp-http.log`), then runs
   Codex on every question with and without the tools, `--runs` times each
   (default 2), `--parallel` at a time (default 3). It prints a report and saves
   `codex_results.json`, plus every run's raw JSON events in `raw/`.

Try it cheaply first:

```powershell
python scripts\codex_ab.py ... --dry-run                     # everything except Codex; prints the codex commands
python scripts\codex_ab.py ... --runs 1 --only overload_callers   # one question, both conditions
```

Re-running resumes: finished runs are kept. Delete `codex_results.json` to start over.

## On your own codebase

```powershell
python scripts\codex_ab.py --codex-config my_codex_eval.toml -P C:\src\mission\mission.gpr -w C:\src\mission -X BUILD=debug
```

The questions are then generated from your own code's overloads and call
sites, which gives the most relevant numbers. `--n-resolve` / `--n-callers` set
how many questions of each kind to generate.

## Options

| option | default | meaning |
|---|---|---|
| `--codex` | `codex` | the codex command or path; npm's `codex.ps1` / `codex.cmd` are run via `node codex.js` |
| `--gnatpro` | `$GNATPRO_PREFIX`, else `C:\GNATPRO\26.1` on Windows | GNAT Pro + Libadalang prefix |
| `--python` | the Python running the script | a Python that can `import libadalang` |
| `--models` | Codex's default | comma-separated, to compare models |
| `--runs`, `--parallel` | 2, 3 | repeats per question and setup; concurrent Codex processes |
| `--only` | all | comma-separated question ids |
| `--n-resolve`, `--n-callers`, `--seed` | 8, 6, 7 | question generation |
| `--price-in`, `--price-cached`, `--price-out` | – | $ per 1M tokens, for a cost column |
| `--skip-xref`, `--dry-run` | off | skip the GNAT accuracy step; skip Codex |
| `--work` | temp folder | scratch dir |

## Reading the report

For each model, setup, and question kind, the report shows:

- **correct:** all expected locations present and no wrong ones;
- **recall:** the fraction of expected locations found;
- **wrong locs:** locations stated that GNAT says are wrong;
- **turns:** tool calls + 1;
- **secs**;
- **in-tok / out-tok:** tokens per question (input includes cached input).

`[N agent errors]` means Codex failed or returned nothing, e.g. auth, network,
or a timeout. Look in `raw/` for the events. To see whether the with-tools runs
really used the tools, look for `lal_mcp INFO tool ...` lines in
`lal-mcp-http.log`.

Notes:

- Codex runs with `--ignore-user-config`, so your `~/.codex/config.toml` doesn't
  affect the comparison. Put the sandbox mode and provider in the eval TOML
  instead. Auth still uses `CODEX_HOME` and the provider's `env_key`.
- The `ada` server is set to `default_tools_approval_mode = "approve"` for the
  eval. Otherwise Codex refuses `run_lal` under `approval_policy = "never"`;
  see [Approvals](../README.md#approvals-why-tool-calls-can-seem-to-hang).
- Event parsing follows `codex exec --json` of codex-cli 0.163. Answers come
  from Codex's final-message file, so they're scored correctly even if a future
  version changes the event format. Only the tool and token columns could be off.

Checked on Windows (codex-cli 0.163, ChatGPT login, unelevated sandbox): 3
questions × both setups, all correct, tools used in the with-tools runs.
