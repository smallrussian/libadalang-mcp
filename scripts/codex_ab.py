"""End-to-end Codex A/B eval for lal-mcp, on Windows or Linux.

  1. the code to test: gnatcoll-core at a pinned commit, or your own project
  2. ground truth: compile with -gnatc into a scratch dir (.ali cross-references)
  3. resolution accuracy vs GNAT (scripts/eval_xref.py; no LLM involved)
  4. questions generated from the ground truth (scripts/make_agent_questions.py)
  5. lal-mcp started in --http mode, then Codex asked every question with and
     without the `ada` tools (scripts/eval_agent.py); scored and reported

HTTP mode means Codex never launches Python itself, which avoids Windows sandbox
"Access is denied (os error 5)" problems, the same setup as interactive use.

    python scripts/codex_ab.py --codex-config my_codex_eval.toml
    python scripts/codex_ab.py --codex-config my_codex_eval.toml -P C:\\src\\mission\\mission.gpr -w C:\\src\\mission -X BUILD=debug
    python scripts/codex_ab.py --codex-config my_codex_eval.toml --dry-run      # everything except Codex

Nothing is written to the project's sources: builds, questions, logs and results go to --work.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
GNATCOLL_URL = "https://github.com/AdaCore/gnatcoll-core.git"
GNATCOLL_COMMIT = "ad1fefda3a7586a3150dba6a919a206003868825"  # the commit docs/metrics.md was measured on
IS_WIN = os.name == "nt"


def step(msg: str) -> None:
    print(f"== {msg}", flush=True)


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=True, **kw)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--codex-config", required=True,
                    help="TOML with codex settings for the eval (copy scripts/codex_eval.example.toml)")
    ap.add_argument("--codex", default="codex", help="codex command or path (npm's codex.ps1/.cmd works)")
    ap.add_argument("--gnatpro", default=os.environ.get("GNATPRO_PREFIX") or (r"C:\GNATPRO\26.1" if IS_WIN else None),
                    help="GNAT Pro + Libadalang install prefix (default: $GNATPRO_PREFIX)")
    ap.add_argument("--python", default=sys.executable, help="Python that can import libadalang (default: this one)")
    ap.add_argument("-P", "--project", help="your root .gpr (default: gnatcoll-core)")
    ap.add_argument("-w", "--workspace", help="root of that project's repository (default: the .gpr's directory)")
    ap.add_argument("-X", dest="scenario", action="append", default=[], metavar="VAR=VALUE")
    ap.add_argument("--work", default=os.path.join(tempfile.gettempdir(), "lal-mcp-codex-eval"),
                    help="scratch directory; re-running resumes")
    ap.add_argument("--models", default="", help="comma-separated (default: the TOML's / Codex's default)")
    ap.add_argument("--runs", type=int, default=2)
    ap.add_argument("--parallel", type=int, default=3)
    ap.add_argument("--n-resolve", type=int, default=8)
    ap.add_argument("--n-callers", type=int, default=6)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--only", help="comma-separated question ids (e.g. to try one first)")
    ap.add_argument("--price-in", type=float)
    ap.add_argument("--price-cached", type=float)
    ap.add_argument("--price-out", type=float)
    ap.add_argument("--skip-xref", action="store_true", help="skip the GNAT accuracy step")
    ap.add_argument("--dry-run", action="store_true", help="do everything except running Codex")
    a = ap.parse_args()

    if not a.gnatpro or not os.path.isdir(a.gnatpro):
        ap.error("--gnatpro (or GNATPRO_PREFIX) must point at the GNAT Pro install")
    gnat_bin = os.path.join(a.gnatpro, "bin")
    env = dict(os.environ, PATH=gnat_bin + os.pathsep + os.environ.get("PATH", ""))
    if not shutil.which("gprbuild", path=env["PATH"]):
        sys.exit(f"gprbuild not found in {gnat_bin}")
    os.makedirs(a.work, exist_ok=True)
    try:
        run([a.python, "-c", "import libadalang, mcp"], env=env)
    except subprocess.CalledProcessError:
        sys.exit(f"{a.python} can't import libadalang and mcp")

    # ------------------------------------------------------------ 1. code
    hand = None
    scenario = list(a.scenario)
    if a.project:
        project = os.path.abspath(a.project)
        workspace = os.path.abspath(a.workspace or os.path.dirname(project))
    else:
        src = os.path.join(a.work, "gnatcoll-core")
        if not os.path.isdir(os.path.join(src, ".git")):
            step(f"fetching gnatcoll-core @ {GNATCOLL_COMMIT[:7]}")
            run(["git", "init", "-q", src])
            run(["git", "-C", src, "fetch", "-q", "--depth", "1", GNATCOLL_URL, GNATCOLL_COMMIT])
            run(["git", "-C", src, "checkout", "-q", GNATCOLL_COMMIT])
        project = os.path.join(src, "core", "gnatcoll_core.gpr")
        workspace = src
        scenario = scenario or [f"GNATCOLL_OS={'windows' if IS_WIN else 'unix'}"]
        hand = os.path.join(HERE, "agent_questions_gnatcoll.json")  # OS-independent files only
    xargs = [f"-X{s}" for s in scenario]
    step(f"project {project} (workspace {workspace}) {' '.join(xargs)}")

    # --------------------------------------------------- 2. ground truth
    ali = os.path.join(a.work, "ali")
    if not any(f.endswith(".ali") for _, _, fs in os.walk(ali) for f in fs):
        step(f"compiling with -gnatc for cross-references (into {ali})")
        with open(os.path.join(a.work, "gprbuild.log"), "w") as log:
            subprocess.run(["gprbuild", "-P", project, *xargs, f"--relocate-build-tree={ali}", "--root-dir=.",
                            "-c", "-k", "-q", "-j0", "--restricted-to-languages=ada", "-cargs:ada", "-gnatc"],
                           cwd=workspace, env=env, stdout=log, stderr=subprocess.STDOUT)
    n_ali = sum(f.endswith(".ali") for _, _, fs in os.walk(ali) for f in fs)
    print(f"   {n_ali} .ali files")
    if not n_ali:
        sys.exit(f"no .ali files; see {os.path.join(a.work, 'gprbuild.log')}")

    # --------------------------------------- 3. resolution accuracy (no LLM)
    if not a.skip_xref and not a.dry_run:
        step("resolution accuracy vs GNAT")
        xref = os.path.join(a.work, "xref.json")
        run([a.python, os.path.join(HERE, "eval_xref.py"), "-P", project, "-w", workspace, *xargs,
             "--ali-dir", ali, "--json", xref], env=env, stdout=subprocess.DEVNULL)
        d = json.load(open(xref))
        g, f = d["goto_definition"], d["find_references"]
        print(f"   goto_definition: {g['same_entity_pct']}% same entity as GNAT over {g['gnat_references']} references")
        print(f"   find_references: recall {f['recall_pct']}%, precision {f['precision_pct']}% "
              f"over {f['entities_sampled']} entities")

    # ------------------------------------------------------- 4. questions
    qgen = os.path.join(a.work, "questions_generated.json")
    run([a.python, os.path.join(HERE, "make_agent_questions.py"), "--ali-dir", ali, "--src", workspace,
         "--out", qgen, "--resolve", str(a.n_resolve), "--callers", str(a.n_callers), "--seed", str(a.seed)],
        env=env, stdout=subprocess.DEVNULL)
    questions = [qgen] + ([hand] if hand else [])
    n_q = sum(len(json.load(open(p))["questions"]) for p in questions)
    step(f"{n_q} questions")

    # --------------------------------------- 5. lal-mcp over HTTP + codex
    port = free_port()
    server_log = os.path.join(a.work, "lal-mcp-http.log")
    step(f"starting lal-mcp --http on port {port} (log: {server_log})")
    server = subprocess.Popen(
        [a.python, os.path.join(REPO, "lal_mcp", "server.py"), "--http", "--port", str(port),
         "-P", project, "-w", workspace, *xargs, "--log-file", server_log],
        cwd=workspace, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 120
        while True:
            if server.poll() is not None:
                sys.exit(f"lal-mcp exited early; see {server_log}")
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
                break
            except OSError:
                if time.monotonic() > deadline:
                    sys.exit(f"lal-mcp did not start listening; see {server_log}")
                time.sleep(0.3)
        mcp_json = os.path.join(a.work, "ada-mcp.json")
        with open(mcp_json, "w") as fh:
            json.dump({"mcpServers": {"ada": {"type": "http", "url": f"http://127.0.0.1:{port}/mcp"}}}, fh, indent=2)

        results = os.path.join(a.work, "codex_results.json")
        cmd = [sys.executable, os.path.join(HERE, "eval_agent.py"), "--agent", "codex", "--codex", a.codex,
               "--codex-config", a.codex_config, "--questions", ",".join(questions), "--cwd", workspace,
               "--mcp-config", mcp_json, "--runs", str(a.runs), "--parallel", str(a.parallel), "--timeout", "900",
               "--json", results, "--resume", results, "--raw-dir", os.path.join(a.work, "raw")]
        for flag, val in (("--models", a.models), ("--only", a.only), ("--price-in", a.price_in),
                          ("--price-cached", a.price_cached), ("--price-out", a.price_out)):
            if val:
                cmd += [flag, str(val)]
        if a.dry_run:
            cmd.append("--dry-run")
        step(f"codex A/B: runs={a.runs} parallel={a.parallel} (results: {results})")
        rc = subprocess.run(cmd).returncode
        return rc
    finally:
        server.terminate()
        try:
            server.wait(timeout=15)
        except subprocess.TimeoutExpired:
            server.kill()


if __name__ == "__main__":
    sys.exit(main())
