"""A/B test: does an agent answer Ada questions better with the lal-mcp tools?

Each question is asked twice, in two conditions that differ only in the tools:
  with_tools: the `ada` MCP server + the agent's normal read-only file access
  no_tools:   the agent's normal read-only file access only
Answers are scored against ground truth (file:line sets, e.g. from GNAT .ali
cross-references): recall of expected locations, and wrong locations stated.

Agents:
  --agent claude  Claude Code headless (`claude -p`). Read/Grep/Glob allowed;
                  shell, edits and web disabled. Reports cost in $.
  --agent codex   OpenAI Codex CLI (`codex exec --json`), read-only sandbox,
                  approvals off. Your ~/.codex/config.toml is NOT used (so an
                  [mcp_servers.ada] entry there can't leak into no_tools): model
                  and provider settings come from --codex-config, a small TOML
                  file (see scripts/codex_eval.example.toml). Auth still comes
                  from CODEX_HOME / the provider's env_key. Codex reports tokens,
                  not $, so pass --price-in/--price-out to get a cost estimate.

In both cases the `ada` server definition is read from --mcp-config, a JSON
file in Claude's .mcp.json format: {"mcpServers": {"ada": {"command": ...,
"args": [...], "env": {...}}}}.

    python scripts/eval_agent.py --agent codex --codex-config codex_eval.toml \
        --questions scripts/agent_questions_generated.json --cwd <repo> \
        --mcp-config ada-mcp.json --runs 2 --parallel 3 --json results.json
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
          "(for example core/src/foo.adb:12). Do not build or compile anything, and do not modify files.")
LOC_RE = re.compile(r"([\w.\-/\\]+\.ad[sb]):(\d+)(?:\s*[-–]\s*(\d+))?")
COMMON_DENY = "Bash Edit Write NotebookEdit WebFetch WebSearch Task Agent"


# ------------------------------------------------------------------ scoring


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


def _run(cmd: list[str], cwd: str, timeout: int, env: dict | None = None,
         stdin_text: str | None = None) -> tuple[str, int | None]:
    try:
        extra = {"input": stdin_text} if stdin_text is not None else {"stdin": subprocess.DEVNULL}
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, env=env, **extra)
        return p.stdout, p.returncode
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        return out, None


# ------------------------------------------------------------------- claude


def run_claude(a, prompt: str, with_tools: bool, model: str | None, raw_path: str | None) -> dict:
    cfg = a.mcp_config if with_tools else a._empty_mcp
    allowed = "mcp__ada__* Read Grep Glob" if with_tools else "Read Grep Glob"
    cmd = [a.claude, "-p", prompt, "--output-format", "stream-json", "--verbose",
           "--mcp-config", cfg, "--strict-mcp-config",
           "--allowedTools", allowed, "--disallowedTools", COMMON_DENY, "--max-turns", "40"]
    if model:
        cmd += ["--model", model]
    t = time.monotonic()
    out, _ = _run(cmd, a.cwd, a.timeout)
    if raw_path:
        with open(raw_path, "w", encoding="utf-8") as f:
            f.write(out)
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


# -------------------------------------------------------------------- codex


def toml_value(v) -> str:
    """Serialize a Python value as a TOML inline value (for `codex -c key=value`)."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return json.dumps(v)  # a JSON string is a valid TOML basic string
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(toml_value(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{" + ", ".join(f"{json.dumps(str(k))} = {toml_value(x)}" for k, x in v.items()) + "}"
    raise TypeError(f"cannot express {type(v).__name__} in TOML")


def codex_command(spec: str) -> list[str]:
    """How to start Codex. npm installs ship codex.cmd / codex.ps1 wrappers on Windows;
    run the underlying `node codex.js` instead, so arguments aren't re-quoted by cmd/PowerShell."""
    import shutil

    path = spec if os.path.isfile(spec) else (shutil.which(spec) or spec)
    if path.lower().endswith(".js"):
        return [shutil.which("node") or "node", path]
    if path.lower().endswith((".cmd", ".ps1", ".bat")) or (os.name == "nt" and not path.lower().endswith(".exe")):
        base = os.path.dirname(os.path.abspath(path))
        js = os.path.join(base, "node_modules", "@openai", "codex", "bin", "codex.js")
        node = os.path.join(base, "node.exe")
        if os.path.isfile(js):
            return [node if os.path.isfile(node) else (shutil.which("node") or "node"), js]
    return [path]


def codex_overrides(base: dict, ada: dict | None) -> list[str]:
    """`-c` arguments: the eval TOML's settings, plus the ada server for with_tools.

    `ada` is either a stdio server ({"command", "args", "env"}) or an HTTP one ({"url"}).
    approval_policy="never" makes Codex refuse (not wait for) tools that need approval,
    so the ada server is set to default_tools_approval_mode="approve" (verified with codex-cli 0.163):
    the eval measures answers, not approval prompts.
    """
    args = []
    for k, v in base.items():
        if k == "mcp_servers":
            continue  # tool availability is controlled here, not by the eval TOML
        args += ["-c", f"{k}={toml_value(v)}"]
    args += ["-c", 'approval_policy="never"']
    if ada is not None:
        if ada.get("url"):
            server = {"url": ada["url"]}
            if ada.get("bearer_token_env_var"):
                server["bearer_token_env_var"] = ada["bearer_token_env_var"]
        else:
            server = {"command": ada["command"], "args": list(ada.get("args", []))}
        if ada.get("env") and not ada.get("url"):
            server["env"] = dict(ada["env"])
        server["default_tools_approval_mode"] = "approve"  # "auto" = per annotations: run_lal would be refused
        server["startup_timeout_sec"] = int(ada.get("startup_timeout_sec", 60))
        server["tool_timeout_sec"] = int(ada.get("tool_timeout_sec", 180))
        args += ["-c", f"mcp_servers.ada={toml_value(server)}"]
    return args


def parse_codex_events(out: str) -> dict:
    """Pull tool calls, token usage and failures out of `codex exec --json` output.

    Tolerant on purpose: event shapes have changed across Codex versions. Raw
    events are saved with --raw-dir so the parse can be checked.
    """
    tools: dict[str, int] = {}
    usage = {"input": 0, "cached": 0, "output": 0}
    failed = None
    turns = 0
    for ln in out.splitlines():
        try:
            ev = json.loads(ln)
        except ValueError:
            continue
        et = str(ev.get("type", ""))
        if et in ("turn.failed", "error"):
            failed = json.dumps(ev)[:300]
        if et == "turn.completed":
            turns += 1
        item = ev.get("item") if isinstance(ev.get("item"), dict) else None
        if item and et == "item.completed":
            it = item.get("type") or item.get("item_type") or "item"
            if it == "mcp_tool_call":
                name = f"mcp:{item.get('server', '?')}.{item.get('tool', '?')}"
            elif it == "command_execution":
                cmd = str(item.get("command", "")).strip().split()
                name = "shell:" + (os.path.basename(cmd[0]) if cmd else "?")
            elif it in ("agent_message", "reasoning"):
                name = None
            else:
                name = it
            if name:
                tools[name] = tools.get(name, 0) + 1
        u = ev.get("usage")
        if isinstance(u, dict) and et == "turn.completed":
            usage["input"] += int(u.get("input_tokens") or 0)
            usage["cached"] += int(u.get("cached_input_tokens") or 0)
            usage["output"] += int(u.get("output_tokens") or 0)
    return {"tools": tools, "usage": usage, "failed": failed, "turns": turns}


def run_codex(a, prompt: str, with_tools: bool, model: str | None, raw_path: str | None) -> dict:
    last = tempfile.NamedTemporaryFile(prefix="codex_last_", suffix=".txt", delete=False)
    last.close()
    cmd = a._codex_cmd + ["exec", "--json", "--ephemeral", "--skip-git-repo-check", "--ignore-user-config",
                          "-s", "read-only", "-C", a.cwd, "-o", last.name]
    cmd += codex_overrides(a._codex_base, a._ada if with_tools else None)
    if model:
        cmd += ["-m", model]
    cmd.append("-")  # prompt on stdin: no Windows command-line quoting of a long multi-line prompt
    t = time.monotonic()
    out, rc = _run(cmd, a.cwd, a.timeout, stdin_text=prompt)
    if raw_path:
        with open(raw_path, "w", encoding="utf-8") as f:
            f.write(out)
    try:
        with open(last.name, encoding="utf-8", errors="replace") as f:
            answer = f.read().strip()
    finally:
        os.unlink(last.name)
    ev = parse_codex_events(out)
    u = ev["usage"]
    cost = None
    if a.price_in is not None and a.price_out is not None:
        # cached input billed at --price-cached (default: same as input)
        p_cached = a.price_cached if a.price_cached is not None else a.price_in
        cost = ((u["input"] - u["cached"]) * a.price_in + u["cached"] * p_cached + u["output"] * a.price_out) / 1e6
    tool_calls = sum(ev["tools"].values())
    return {
        "answer": answer,
        "ok": rc == 0 and bool(answer) and not ev["failed"],
        "error": ev["failed"] or (None if rc == 0 else f"exit code {rc}"),
        "seconds": round(time.monotonic() - t, 1),
        "turns": tool_calls + 1,  # Codex doesn't report model turns; tool calls + final answer
        "cost_usd": cost,
        "input_tokens": u["input"],
        "cached_input_tokens": u["cached"],
        "output_tokens": u["output"],
        "tool_calls": ev["tools"],
    }


# --------------------------------------------------------------------- main


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--agent", choices=("claude", "codex"), default="claude")
    ap.add_argument("--questions", required=True, help="comma-separated question JSON files")
    ap.add_argument("--cwd", required=True, help="repository the agent works in")
    ap.add_argument("--mcp-config", required=True, help="JSON with mcpServers.ada (Claude .mcp.json format)")
    ap.add_argument("--claude", default="claude", help="claude executable")
    ap.add_argument("--codex", default="codex",
                    help="codex command or path (npm's codex.cmd/.ps1 or codex.js are run via node)")
    ap.add_argument("--codex-config", help="TOML with model/provider settings for codex (required for --agent codex)")
    ap.add_argument("--price-in", type=float, help="codex: $ per 1M input tokens")
    ap.add_argument("--price-cached", type=float, help="codex: $ per 1M cached input tokens")
    ap.add_argument("--price-out", type=float, help="codex: $ per 1M output tokens")
    ap.add_argument("--models", default="", help="comma-separated (default: the agent's default model)")
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--parallel", type=int, default=1, help="concurrent agent processes")
    ap.add_argument("--timeout", type=int, default=600, help="seconds per question")
    ap.add_argument("--only", help="comma-separated question ids")
    ap.add_argument("--json", help="write results here (updated after every run)")
    ap.add_argument("--resume", help="previous --json results; skip runs already there")
    ap.add_argument("--raw-dir", help="save each run's raw agent output here")
    ap.add_argument("--dry-run", action="store_true", help="print the commands for the first question and exit")
    ap.add_argument("--with-tools-guidance", choices=("auto", "agents", "none"), default="auto",
                    help="with_tools runs only: 'agents' prepends docs/AGENTS-snippet.md, i.e. the AGENTS.md "
                         "guidance you would ship with the tools; 'auto' = agents for codex (which does not "
                         "act on the server's instructions by itself), none for claude")
    a = ap.parse_args()

    with open(a.mcp_config, encoding="utf-8") as f:
        a._ada = json.load(f)["mcpServers"]["ada"]
    a._empty_mcp = os.path.join(tempfile.mkdtemp(), "no_mcp.json")
    with open(a._empty_mcp, "w") as f:
        json.dump({"mcpServers": {}}, f)
    if a.agent == "codex":
        if not a.codex_config:
            ap.error("--agent codex needs --codex-config (see scripts/codex_eval.example.toml)")
        import tomllib

        with open(a.codex_config, "rb") as f:
            a._codex_base = tomllib.load(f)
    a._codex_cmd = codex_command(a.codex) if a.agent == "codex" else []
    mode = a.with_tools_guidance
    if mode == "auto":
        mode = "agents" if a.agent == "codex" else "none"
    a._guidance = ""
    if mode == "agents":
        snippet = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs",
                                    "AGENTS-snippet.md"), encoding="utf-8").read()
        snippet = re.sub(r"<!--.*?-->", "", snippet, flags=re.S).strip()
        a._guidance = f"Repository instructions (AGENTS.md):\n{snippet}\n\nTask:\n"
    print(f"with_tools guidance: {mode}", flush=True)
    runner = run_codex if a.agent == "codex" else run_claude

    qs = []
    for path in a.questions.split(","):
        for q in json.load(open(path, encoding="utf-8"))["questions"]:
            q.setdefault("kind", "hand")
            qs.append(q)
    if a.only:
        keep = set(a.only.split(","))
        qs = [q for q in qs if q["id"] in keep]

    if a.dry_run:
        prompt = qs[0]["question"] + FORMAT
        for with_tools in (True, False):
            print(f"# {'with_tools' if with_tools else 'no_tools'}")
            if a.agent == "codex":
                cmd = a._codex_cmd + ["exec", "--json", "--ephemeral", "--skip-git-repo-check",
                                      "--ignore-user-config", "-s", "read-only", "-C", a.cwd,
                                      "-o", "<last-message-file>"]
                cmd += codex_overrides(a._codex_base, a._ada if with_tools else None) + ["-", "<<< prompt on stdin"]
            else:
                cmd = [a.claude, "-p", prompt, "--mcp-config", a.mcp_config if with_tools else a._empty_mcp,
                       "--strict-mcp-config"]
            print(subprocess.list2cmdline(cmd) if os.name == "nt" else " ".join(map(_shq, cmd)))
        return

    if a.raw_dir:
        os.makedirs(a.raw_dir, exist_ok=True)
    models = [m for m in a.models.split(",") if m] or [None]
    rows: list[dict] = []
    if a.resume and os.path.exists(a.resume):
        rows = json.load(open(a.resume, encoding="utf-8"))
    qmap = {q["id"]: q for q in qs}
    rows = [r for r in rows if r.get("ok") and r["id"] in qmap and r.get("agent", "claude") == a.agent]
    for r in rows:  # re-score reused answers with the current rules
        r.update(score(qmap[r["id"]], r["answer"]), kind=qmap[r["id"]]["kind"])
    done = {(r["model"], r["id"], r["condition"], r["run"]) for r in rows}
    conditions = ("with_tools", "no_tools")
    jobs = [(m, q, cond, run) for m in models for q in qs for cond in conditions for run in range(a.runs)
            if (m or "default", q["id"], cond, run) not in done]
    print(f"{a.agent}: {len(jobs)} runs ({len(done)} reused)", flush=True)

    import threading
    from concurrent.futures import ThreadPoolExecutor

    lock = threading.Lock()

    def one(job):
        m, q, cond, run = job
        raw = os.path.join(a.raw_dir, f"{a.agent}_{m or 'default'}_{q['id']}_{cond}_{run}.jsonl") if a.raw_dir else None
        prompt = (a._guidance if cond == "with_tools" else "") + q["question"] + FORMAT
        r = runner(a, prompt, cond == "with_tools", m, raw)
        r.update(score(q, r["answer"]), id=q["id"], kind=q["kind"], condition=cond, run=run,
                 model=m or "default", agent=a.agent, guidance=bool(a._guidance and cond == "with_tools"))
        with lock:
            rows.append(r)
            cost = f"${r['cost_usd']:.3f}" if r.get("cost_usd") is not None else f"{r['input_tokens']}tok"
            err = f"  ERROR {r['error'][:80]}" if r.get("error") else ""
            print(f"{r['model']:<10} {q['id'][:34]:<34} {cond:<10} ok={r['correct']!s:<5} recall={r['recall']:.2f} "
                  f"wrong={len(r['wrong'])} turns={r['turns']} {cost} {r['seconds']}s{err}", flush=True)
            if a.json:
                with open(a.json, "w", encoding="utf-8") as f:
                    json.dump(rows, f, indent=2)

    with ThreadPoolExecutor(max_workers=max(1, a.parallel)) as ex:
        list(ex.map(one, jobs))
    report(rows)


def _shq(s: str) -> str:
    import shlex

    return shlex.quote(s)


def report(rows: list[dict]) -> None:
    def line(label, rs):
        if not rs:
            return
        n = len(rs)
        ok = sum(r["correct"] for r in rs)
        costs = [r["cost_usd"] for r in rs if r.get("cost_usd") is not None]
        cost = (f"  cost ${statistics.mean(costs):.3f}/q (total ${sum(costs):.2f})" if len(costs) == n else "")
        failed = sum(1 for r in rs if not r.get("ok"))
        print(f"  {label:<30} correct {ok:>3}/{n:<3} ({100 * ok / n:5.1f}%)"
              f"  recall {statistics.mean(r['recall'] for r in rs):.2f}"
              f"  wrong locs {sum(len(r['wrong']) for r in rs):>3}"
              f"  turns {statistics.mean((r['turns'] or 0) for r in rs):4.1f}"
              f"  secs {statistics.mean(r['seconds'] for r in rs):5.1f}"
              f"  in-tok {statistics.mean((r['input_tokens'] or 0) for r in rs):8.0f}"
              f"  out-tok {statistics.mean((r['output_tokens'] or 0) for r in rs):6.0f}"
              + cost + (f"  [{failed} agent errors]" if failed else ""))

    for agent in sorted({r.get("agent", "claude") for r in rows}):
        for m in sorted({r["model"] for r in rows if r.get("agent", "claude") == agent}):
            sel = [r for r in rows if r.get("agent", "claude") == agent and r["model"] == m]
            print()
            print(f"{agent} / model: {m}")
            for cond in ("with_tools", "no_tools"):
                line(cond, [r for r in sel if r["condition"] == cond])
            for kind in sorted({r["kind"] for r in sel}):
                for cond in ("with_tools", "no_tools"):
                    line(f"{kind} / {cond}", [r for r in sel if r["condition"] == cond and r["kind"] == kind])


if __name__ == "__main__":
    sys.exit(main())
