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
    """Recall over expected groups (a group is satisfied by any of its locations, e.g. a
    subprogram's spec or body), plus wrong locations in the files that matter."""
    got = locations(answer)
    groups = [{e.lower() for e in g} for g in q.get("expected_groups", [[e] for e in q["expected"]])]
    known = set().union(*groups) | {e.lower() for e in q.get("allow", [])}
    fp_files = {f.lower() for f in q.get("fp_files", [])}
    hit = [bool(g & got) for g in groups]
    wrong = {g for g in got - known if g.split(":")[0] in fp_files}
    return {"recall": sum(hit) / len(groups), "wrong": sorted(wrong),
            "missed": sorted(sorted(g)[0] for g, h in zip(groups, hit) if not h),
            "correct": all(hit) and not wrong}


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
    ap.add_argument("--models", default="", help="comma-separated, e.g. sonnet,haiku (default: CLI default)")
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--parallel", type=int, default=1, help="concurrent claude processes")
    ap.add_argument("--timeout", type=int, default=600)
    ap.add_argument("--only", help="comma-separated question ids")
    ap.add_argument("--json", help="write raw results here")
    ap.add_argument("--resume", help="previous --json results; skip runs already there")
    a = ap.parse_args()

    qs = []
    for path in a.questions.split(","):
        for q in json.load(open(path, encoding="utf-8"))["questions"]:
            q.setdefault("kind", "hand")
            qs.append(q)
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
    models = [m for m in a.models.split(",") if m] or [None]
    rows: list[dict] = []
    if a.resume and os.path.exists(a.resume):
        rows = json.load(open(a.resume, encoding="utf-8"))
    qmap = {q["id"]: q for q in qs}
    rows = [r for r in rows if r.get("ok") and r["id"] in qmap]
    for r in rows:  # re-score reused answers with the current rules
        r.update(score(qmap[r["id"]], r["answer"]), kind=qmap[r["id"]]["kind"])
    done = {(r["model"], r["id"], r["condition"], r["run"]) for r in rows}
    jobs = [(m, q, cond, run) for m in models for q in qs for cond in conditions for run in range(a.runs)
            if (m or "default", q["id"], cond, run) not in done]
    print(f"{len(jobs)} runs ({len(done)} reused)", flush=True)

    import threading
    from concurrent.futures import ThreadPoolExecutor

    lock = threading.Lock()

    def one(job):
        m, q, cond, run = job
        cfg, allowed = conditions[cond]
        r = run_claude(a.claude, q["question"] + FORMAT, a.cwd, cfg, allowed, m, a.timeout)
        r.update(score(q, r["answer"]), id=q["id"], kind=q["kind"], condition=cond, run=run, model=m or "default")
        with lock:
            rows.append(r)
            print(f"{r['model']:<7} {q['id'][:34]:<34} {cond:<10} ok={r['correct']!s:<5} recall={r['recall']:.2f} "
                  f"wrong={len(r['wrong'])} turns={r['turns']} ${r['cost_usd'] or 0:.3f} {r['seconds']}s", flush=True)
            if a.json:
                with open(a.json, "w", encoding="utf-8") as f:
                    json.dump(rows, f, indent=2)

    with ThreadPoolExecutor(max_workers=max(1, a.parallel)) as ex:
        list(ex.map(one, jobs))
    report(rows)


def report(rows: list[dict]) -> None:
    def line(label, rs):
        if not rs:
            return
        n = len(rs)
        print(f"  {label:<28} correct {sum(r['correct'] for r in rs):>3}/{n:<3} ({100 * sum(r['correct'] for r in rs) / n:5.1f}%)"
              f"  recall {statistics.mean(r['recall'] for r in rs):.2f}"
              f"  wrong locs {sum(len(r['wrong']) for r in rs):>3}"
              f"  turns {statistics.mean((r['turns'] or 0) for r in rs):4.1f}"
              f"  secs {statistics.mean(r['seconds'] for r in rs):5.1f}"
              f"  cost ${statistics.mean((r['cost_usd'] or 0) for r in rs):.3f}/q (total ${sum((r['cost_usd'] or 0) for r in rs):.2f})")

    for m in sorted({r["model"] for r in rows}):
        print()
        print(f"model: {m}")
        for cond in ("with_tools", "no_tools"):
            line(cond, [r for r in rows if r["model"] == m and r["condition"] == cond])
        for kind in sorted({r["kind"] for r in rows}):
            for cond in ("with_tools", "no_tools"):
                line(f"{kind} / {cond}", [r for r in rows if r["model"] == m and r["condition"] == cond and r["kind"] == kind])


if __name__ == "__main__":
    sys.exit(main())
