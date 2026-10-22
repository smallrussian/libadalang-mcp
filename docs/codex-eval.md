# Measuring lal-mcp with Codex

`scripts/codex_ab.sh` measures whether Codex + your GPT model answers Ada
questions better with the `ada` tools than without them. It's the same
methodology as the Claude numbers in [metrics.md](metrics.md), so the results
are directly comparable.

Each question is asked twice through `codex exec`:

- **with tools:** the `ada` MCP server plus Codex's normal read-only shell
  (`rg`, `sed`, `cat`...);
- **no tools:** the read-only shell only.

Both runs use the same model, provider and sandbox (`-s read-only`, approvals
off), so the tools are the only difference. Answers are scored against the GNAT
compiler's own cross-references: every expected `file:line` must be present,
with no wrong locations.

## Run it (Linux)

```bash
cp scripts/codex_eval.example.toml ~/codex_eval.toml   # fill in model + [model_providers.*]
export PROXY_API_KEY=...                               # whatever env_key your TOML names

GNATPRO_PREFIX=/opt/gnatpro/26.1 \
LAL_PYTHON=/path/to/venv/bin/python \
CODEX_EVAL_TOML=~/codex_eval.toml \
bash scripts/codex_ab.sh
```

What it does, all under `$WORK` (default `/tmp/lal-mcp-codex-eval`):

1. Fetches gnatcoll-core at the commit `metrics.md` was measured on (one commit,
   shallow), or uses your project (see below).
2. Compiles with `gprbuild -gnatc` into a scratch build tree, to get GNAT's
   cross-references (`.ali`). Your sources aren't written to.
3. Prints resolution accuracy against GNAT. No LLM is involved; skip it with `SKIP_XREF=1`.
4. Samples questions from GNAT's data with a fixed seed:
   - which overload a call resolves to;
   - every call site of one particular overload.

   With gnatcoll-core, the 5 hand-written questions are added too.
5. Runs Codex on every question, with and without the tools, `RUNS` times
   each (default 2), `PARALLEL` at a time (default 3). It prints a report and
   saves `codex_results.json`, plus every run's raw JSON events in `raw/`.

Check the setup first without spending tokens. `DRY_RUN=1` does steps 1–4 and
prints the two `codex exec` command lines for one question:

```bash
DRY_RUN=1 GNATPRO_PREFIX=... LAL_PYTHON=... CODEX_EVAL_TOML=... bash scripts/codex_ab.sh
```

Then run one question for real before the full set:

```bash
python scripts/eval_agent.py --agent codex --codex-config ~/codex_eval.toml \
  --questions /tmp/lal-mcp-codex-eval/questions_generated.json --only <an id from that file> \
  --cwd /tmp/lal-mcp-codex-eval/gnatcoll-core --mcp-config /tmp/lal-mcp-codex-eval/ada-mcp.json \
  --raw-dir /tmp/lal-mcp-codex-eval/raw
```

## On your own codebase

```bash
PROJECT_GPR=/ws/mission.gpr WORKSPACE=/ws SCENARIO="-XBUILD=debug" \
GNATPRO_PREFIX=... LAL_PYTHON=... CODEX_EVAL_TOML=... bash scripts/codex_ab.sh
```

The questions are then generated from your own code's overloads and call
sites, which gives the most relevant numbers. `N_RESOLVE` / `N_CALLERS` set how
many questions of each kind to generate.

## Knobs

| variable | default | meaning |
|---|---|---|
| `MODELS` | the TOML's `model` | comma-separated, to compare models |
| `RUNS` | 2 | repeats per question and setup |
| `PARALLEL` | 3 | concurrent `codex exec` processes. Mind your proxy's rate limits |
| `PRICE_IN`, `PRICE_CACHED`, `PRICE_OUT` | – | $ per 1M tokens, for a cost column (Codex reports tokens, not $) |
| `N_RESOLVE`, `N_CALLERS`, `SEED` | 8, 6, 7 | question generation |
| `WORK` | `/tmp/lal-mcp-codex-eval` | scratch dir; re-running resumes and skips finished runs |
| `CODEX` | `codex` | the codex executable |

## Reading the report

For each model, setup, and question kind, the report shows:

- **correct:** all expected locations present and no wrong ones;
- **recall:** the fraction of expected locations found;
- **wrong locs:** locations stated that GNAT says are wrong;
- **turns:** tool calls + 1;
- **secs**;
- **in-tok / out-tok:** tokens per question (input includes cached input).

`[N agent errors]` means Codex failed or returned nothing, e.g. auth, network,
or a timeout. Look in `raw/` for the events.

Notes:

- Codex is run with `--ignore-user-config`, so your `~/.codex/config.toml`
  (including any `[mcp_servers.ada]` in it) doesn't affect the comparison. Auth
  still uses `CODEX_HOME` and the provider's `env_key`.
- Event parsing follows `codex exec --json` as of codex-cli 0.163
  (`item.completed` with `mcp_tool_call` / `command_execution` items, and
  `turn.completed` with `usage`). If a newer Codex changes the format, answers
  are still scored (they come from `-o`), but the tool and token columns may be
  off. Check `raw/`.
- With tools, the first question also pays the project load inside the `ada`
  server (about 1 s for gnatcoll-core; more on a large codebase).
  `tool_timeout_sec` is set to 180.
