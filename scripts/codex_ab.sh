#!/usr/bin/env bash
# End-to-end Codex A/B eval for lal-mcp (Linux).
#
#   1. get the code to test: gnatcoll-core at a pinned commit, or your own project
#   2. ground truth: compile with -gnatc into a scratch dir (.ali cross-references)
#   3. resolution accuracy vs GNAT (scripts/eval_xref.py; no LLM involved)
#   4. generate questions from the ground truth (scripts/make_agent_questions.py)
#   5. ask Codex every question with and without the ada tools; score; report
#
# Required:
#   GNATPRO_PREFIX   GNAT Pro + Libadalang install prefix (its bin/ goes on PATH)
#   LAL_PYTHON       a Python that can `import libadalang` (and has `mcp`)
#   CODEX_EVAL_TOML  model/provider settings for codex (copy scripts/codex_eval.example.toml)
#   + whatever API key env var that TOML's env_key names
#
# Optional:
#   PROJECT_GPR      your own root project. Default: gnatcoll-core's core/gnatcoll_core.gpr
#   WORKSPACE        root of that project's repository (default: dirname of PROJECT_GPR)
#   SCENARIO         -X arguments for that project, e.g. "-XBUILD=debug -XOS=linux"
#   WORK             scratch directory (default: /tmp/lal-mcp-codex-eval). Nothing is written to your sources.
#   CODEX            codex executable (default: codex)
#   MODELS           comma-separated models (default: the TOML's model)
#   RUNS             runs per question per condition (default 2)
#   PARALLEL         concurrent codex processes (default 3)
#   N_RESOLVE / N_CALLERS   generated questions of each kind (default 8 / 6)
#   SEED             question sampling seed (default 7)
#   PRICE_IN / PRICE_CACHED / PRICE_OUT   $ per 1M tokens, for a cost estimate
#   SKIP_XREF=1      skip step 3
#   DRY_RUN=1        stop after printing the codex command lines for one question
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(dirname "$here")"

: "${GNATPRO_PREFIX:?set GNATPRO_PREFIX}"
: "${LAL_PYTHON:?set LAL_PYTHON (a python that can import libadalang)}"
: "${CODEX_EVAL_TOML:?set CODEX_EVAL_TOML (copy scripts/codex_eval.example.toml)}"
WORK="${WORK:-/tmp/lal-mcp-codex-eval}"
CODEX="${CODEX:-codex}"
RUNS="${RUNS:-2}"
PARALLEL="${PARALLEL:-3}"
N_RESOLVE="${N_RESOLVE:-8}"
N_CALLERS="${N_CALLERS:-6}"
SEED="${SEED:-7}"
GNATCOLL_COMMIT="ad1fefda3a7586a3150dba6a919a206003868825"   # the commit docs/metrics.md was measured on (full hash: GitHub only fetches those)

export PATH="$GNATPRO_PREFIX/bin:$PATH"
mkdir -p "$WORK"
command -v gprbuild >/dev/null || { echo "gprbuild not found under $GNATPRO_PREFIX/bin" >&2; exit 1; }
if [ -z "${DRY_RUN:-}" ]; then
    command -v "$CODEX" >/dev/null || { echo "codex not found ($CODEX)" >&2; exit 1; }
fi
"$LAL_PYTHON" -c "import libadalang, mcp" || { echo "LAL_PYTHON can't import libadalang and mcp" >&2; exit 1; }

# ---------------------------------------------------------------- 1. code
hand_questions=""
if [ -z "${PROJECT_GPR:-}" ]; then
    src="$WORK/gnatcoll-core"
    if [ ! -d "$src/.git" ]; then
        echo "== fetching gnatcoll-core @ $GNATCOLL_COMMIT"
        git init -q "$src"
        git -C "$src" fetch -q --depth 1 https://github.com/AdaCore/gnatcoll-core.git "$GNATCOLL_COMMIT" \
            || git -C "$src" fetch -q https://github.com/AdaCore/gnatcoll-core.git
        git -C "$src" checkout -q "$GNATCOLL_COMMIT"
    fi
    PROJECT_GPR="$src/core/gnatcoll_core.gpr"
    WORKSPACE="$src"
    SCENARIO="${SCENARIO:--XGNATCOLL_OS=unix}"
    hand_questions="$here/agent_questions_gnatcoll.json"   # OS-independent files only
fi
WORKSPACE="${WORKSPACE:-$(dirname "$PROJECT_GPR")}"
SCENARIO="${SCENARIO:-}"
read -r -a xargs_arr <<< "$SCENARIO"
echo "== project $PROJECT_GPR (workspace $WORKSPACE) ${SCENARIO}"

# ------------------------------------------------------- 2. ground truth
ali="$WORK/ali"
if [ ! -d "$ali" ] || [ -z "$(find "$ali" -name '*.ali' -print -quit)" ]; then
    echo "== compiling with -gnatc for cross-references (into $ali)"
    (cd "$WORKSPACE" && gprbuild -P "$PROJECT_GPR" "${xargs_arr[@]}" --relocate-build-tree="$ali" --root-dir=. \
        -c -k -q -j0 --restricted-to-languages=ada -cargs:ada -gnatc >"$WORK/gprbuild.log" 2>&1) || true
fi
n_ali=$(find "$ali" -name '*.ali' | wc -l)
echo "   $n_ali .ali files (log: $WORK/gprbuild.log)"
[ "$n_ali" -gt 0 ] || { echo "no .ali files produced; see $WORK/gprbuild.log" >&2; exit 1; }

# ------------------------------------------- 3. resolution accuracy (no LLM)
if [ -z "${SKIP_XREF:-}" ] && [ -z "${DRY_RUN:-}" ]; then
    echo "== resolution accuracy vs GNAT"
    "$LAL_PYTHON" "$here/eval_xref.py" -P "$PROJECT_GPR" -w "$WORKSPACE" "${xargs_arr[@]}" --ali-dir "$ali" \
        --json "$WORK/xref.json" >/dev/null
    "$LAL_PYTHON" -c "
import json; d = json.load(open('$WORK/xref.json'))
g, f = d['goto_definition'], d['find_references']
print(f\"   goto_definition: {g['same_entity_pct']}% same entity as GNAT over {g['gnat_references']} references\")
print(f\"   find_references: recall {f['recall_pct']}%, precision {f['precision_pct']}% over {f['entities_sampled']} entities\")"
fi

# ----------------------------------------------------------- 4. questions
qgen="$WORK/questions_generated.json"
"$LAL_PYTHON" "$here/make_agent_questions.py" --ali-dir "$ali" --src "$WORKSPACE" --out "$qgen" \
    --resolve "$N_RESOLVE" --callers "$N_CALLERS" --seed "$SEED" >/dev/null
questions="$qgen${hand_questions:+,$hand_questions}"
echo "== questions: $(LAL_Q="$questions" "$LAL_PYTHON" -c "
import json, os; print(sum(len(json.load(open(p))['questions']) for p in os.environ['LAL_Q'].split(',')))")"

# ------------------------------------------------- 5. codex with/without
mcp_json="$WORK/ada-mcp.json"
"$LAL_PYTHON" - "$mcp_json" "$repo/lal_mcp/server.py" "$PROJECT_GPR" "$WORKSPACE" "$GNATPRO_PREFIX" ${SCENARIO} <<'PY'
import json, sys, shutil
out, server, gpr, ws, prefix, *scen = sys.argv[1:]
json.dump({"mcpServers": {"ada": {
    "command": sys.executable,
    "args": [server, "-P", gpr, "-w", ws, *scen],
    "env": {"PATH": f"{prefix}/bin:/usr/bin:/bin"},
}}}, open(out, "w"), indent=2)
PY

extra=()
[ -n "${MODELS:-}" ] && extra+=(--models "$MODELS")
[ -n "${PRICE_IN:-}" ] && extra+=(--price-in "$PRICE_IN")
[ -n "${PRICE_CACHED:-}" ] && extra+=(--price-cached "$PRICE_CACHED")
[ -n "${PRICE_OUT:-}" ] && extra+=(--price-out "$PRICE_OUT")
[ -n "${DRY_RUN:-}" ] && extra+=(--dry-run)

echo "== codex A/B: runs=$RUNS parallel=$PARALLEL (results: $WORK/codex_results.json, raw events: $WORK/raw/)"
"$LAL_PYTHON" "$here/eval_agent.py" --agent codex --codex "$CODEX" --codex-config "$CODEX_EVAL_TOML" \
    --questions "$questions" --cwd "$WORKSPACE" --mcp-config "$mcp_json" \
    --runs "$RUNS" --parallel "$PARALLEL" --timeout 900 \
    --json "$WORK/codex_results.json" --resume "$WORK/codex_results.json" --raw-dir "$WORK/raw" \
    "${extra[@]}" | tee "$WORK/codex_report.txt"
