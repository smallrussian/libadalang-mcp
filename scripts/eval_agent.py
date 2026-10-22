"""A/B test: does an agent answer Ada questions better with the lal-mcp tools?

Runs Claude Code headless (`claude -p`) on each question twice:
  with_tools: the `ada` MCP server + Read/Grep/Glob
  no_tools:   Read/Grep/Glob only
Shell, edits and web access are disabled in both. Answers are scored against
ground truth (path:line sets, e.g. from GNAT .ali cross-references): recall of
expected locations, and wrong locations in the files that matter.

    python scripts/eval_agent.py --questions scripts/agent_questions_gnatcoll.json \
        --cwd <repo checkout> --mcp-config <config with an "ada" server> [--model sonnet] [--runs 1]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import subprocess
import sys
import tempfile
import time

FORMAT = ("\n\nAnswer concisely. Give every location as file:line on its own line "
          "(for example core/src/foo.adb:12). Do not build or compile anything.")
LOC_RE = re.compile(r"([\w.\-/\\]+\.ad[sb]):(\d+)(?:\s*[-–]\s*(\d+))?")
COMMON_DENY = "Bash Edit Write NotebookEdit WebFetch WebSearch Task Agent"


def locations(text: str) -> set[str]:
    out = set()
    for m in LOC_RE.finditer(text):
        base = os.path.basename(m.group(1).replace("\\", "/")).lower()
        a = int(m.group(2))
        b = int(m.group(3)) if m.group(3) else a
        for ln in range(a, min(b, a + 20) + 1):
            out.add(f"{base}:{ln}")
    return out


def score(q: dict, answer: str) -> dict:
    got = locations(answer)
    exp = {e.lower() for e in q["expected"]}
    allow = {e.lower() for e in q.get("allow", [])}
    fp_files = {f.lower() for f in q.get("fp_files", [])}
    hits = exp & got
    wrong = {g for g in got - exp - allow if g.split(":")[0] in fp_files}
    return {"recall": len(hits) / len(exp), "wrong": sorted(wrong), "missed": sorted(exp - got),
            "correct": hits == exp and not wrong}


def run_claude(claude: str, prompt: str, cwd: str, mcp_config: str, allowed: str, model: str | None,
               timeout: int) -> dict:
    cmd = [claude, "-p", prompt, "--output-format", "stream-json", "--verbose",
           "--mcp-config", mcp_config, "--strict-mcp-config",
           "--allowedTools", allowed, "--disallowedTools", COMMON_DENY, "--max-turns", "40"]
    if model:
        cmd += ["--model", model]
    t = time.monotonic()
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, stdin=subprocess.DEVNULL)
        out = p.stdout
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
    tools: dict[str, int] = {}
    result = None
    for ln in out.splitlines():
        try:
            ev = json.loads(ln)
        except ValueError:
            continue
        if ev.get("type") == "assistant":
            for c in ev["message"].get("content", []):
                if c.get("type") == "tool_use":
                    tools[c["name"]] = tools.get(c["name"], 0) + 1
        elif ev.get("type") == "result":
            result = ev
    usage = (result or {}).get("usage") or {}
    return {
        "answer": (result or {}).get("result") or "",
        "ok": bool(result) and not (result or {}).get("is_error"),
        "seconds": round(time.monotonic() - t, 1),
        "turns": (result or {}).get("num_turns"),
        "cost_usd": (result or {}).get("total_cost_usd"),
        "input_tokens": (usage.get("input_tokens") or 0) + (usage.get("cache_read_input_tokens") or 0)
                        + (usage.get("cache_creation_input_tokens") or 0),
        "output_tokens": usage.get("output_tokens"),
        "tool_calls": tools,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--questions", required=True)
    ap.add_argument("--cwd", required=True, help="repository the agent works in")
    ap.add_argument("--mcp-config", required=True, help="MCP config with the 'ada' server")
    ap.add_argument("--claude", default="claude")
    ap.add_argument("--model")
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--timeout", type=int, default=600)
    ap.add_argument("--only", help="comma-separated question ids")
    ap.add_argument("--json", help="write raw results here")
    a = ap.parse_args()

    qs = json.load(open(a.questions, encoding="utf-8"))["questions"]
    if a.only:
        keep = set(a.only.split(","))
        qs = [q for q in qs if q["id"] in keep]
    empty = os.path.join(tempfile.mkdtemp(), "no_mcp.json")
    with open(empty, "w") as f:
        json.dump({"mcpServers": {}}, f)
    conditions = {
        "with_tools": (a.mcp_config, "mcp__ada__* Read Grep Glob"),
        "no_tools": (empty, "Read Grep Glob"),
    }
    rows = []
    for q in qs:
        for cond, (cfg, allowed) in conditions.items():
            for run in range(a.runs):
                r = run_claude(a.claude, q["question"] + FORMAT, a.cwd, cfg, allowed, a.model, a.timeout)
                r.update(score(q, r["answer"]), id=q["id"], condition=cond, run=run)
                rows.append(r)
                print(f"{q['id']:<20} {cond:<11} correct={r['correct']!s:<5} recall={r['recall']:.2f} "
                      f"wrong={len(r['wrong'])} turns={r['turns']} in_tok={r['input_tokens']} "
                      f"cost=${r['cost_usd'] or 0:.3f} {r['seconds']}s tools={r['tool_calls']}", flush=True)

    print("\nsummary")
    print(f"{'condition':<11} {'correct':>9} {'recall':>7} {'wrong locs':>10} {'med input tok':>13} "
          f"{'med cost $':>10} {'med secs':>8}")
    for cond in conditions:
        rs = [r for r in rows if r["condition"] == cond]
        if not rs:
            continue
        print(f"{cond:<11} {sum(r['correct'] for r in rs):>4}/{len(rs):<4} "
              f"{statistics.mean(r['recall'] for r in rs):>7.2f} {sum(len(r['wrong']) for r in rs):>10} "
              f"{statistics.median(r['input_tokens'] for r in rs):>13.0f} "
              f"{statistics.median((r['cost_usd'] or 0) for r in rs):>10.3f} "
              f"{statistics.median(r['seconds'] for r in rs):>8.1f}")
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(rows, f, indent=2)


if __name__ == "__main__":
    sys.exit(main())
