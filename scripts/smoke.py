"""Smoke test: launch the server over stdio, call every tool, print response sizes.

For each call, "files" is the total size of the distinct source files the answer
points into: what an agent would otherwise have had to open to get the same facts.

    python scripts/smoke.py                       # fixture project
    python scripts/smoke.py --schemas             # raw tools/list JSON from the wire
    python scripts/smoke.py --gnatcoll PATH       # a gnatcoll-core checkout
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(ROOT, "lal_mcp", "server.py")
FIXTURE = os.path.join(ROOT, "tests", "fixture")

FIXTURE_CALLS = [
    ("find_unit", {"name": "Vehicle.Engine"}),
    ("package_files", {"name": "Vehicle"}),
    ("package_files", {"name": "Vehicle.Engine", "children": False, "deps": True}),
    ("outline", {"target": "Vehicle"}),
    ("outline", {"target": "src/vehicle__engine.2.ada"}),
    ("goto_definition", {"file": "src/main_proc.2.ada", "line": 14, "column": 4}),
    ("goto_definition", {"file": "src/main_proc.2.ada", "line": 10, "column": 19}),
    ("goto_definition", {"file": "src/bad_ref.2.ada", "line": 7, "column": 12}),
    ("find_references", {"file": "src/vehicle.1.ada", "line": 14, "column": 14}),
    ("run_lal", {"code": 'for d in unit("Vehicle").root.findall(lal.BasicSubpDecl):\n'
                         '    print(rel(d), d.p_defining_name.text)'}),
    ("api_help", {"name": "WithClause"}),
    ("dump", {"target": "src/vehicle.1.ada", "start_line": 8, "end_line": 11, "max_depth": 3}),
]

GNATCOLL_CALLS = [
    ("find_unit", {"name": "GNATCOLL.Strings_Impl"}),
    ("package_files", {"name": "GNATCOLL.OS", "specs_only": True}),
    ("package_files", {"name": "GNATCOLL.JSON", "children": False, "deps": True, "specs_only": True}),
    ("outline", {"target": "GNATCOLL.JSON"}),
    ("outline", {"target": "GNATCOLL.Traces"}),
    ("goto_definition", {"file": "core/src/gnatcoll-json.adb", "line": 0, "column": 0}),  # filled below
    ("find_references", {"file": "core/src/gnatcoll-json.ads", "line": 0, "column": 0, "limit": 30}),
]


class Wire:
    """Minimal JSON-RPC client over the server's stdio, to see exactly what is on the wire."""

    def __init__(self, args: list[str]):
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        self.p = subprocess.Popen([sys.executable, SERVER, *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, env=env, cwd=ROOT)
        self.next_id = 0

    def send(self, method: str, params: dict | None = None, notify: bool = False) -> dict | None:
        msg: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        if not notify:
            self.next_id += 1
            msg["id"] = self.next_id
        self.p.stdin.write((json.dumps(msg) + "\n").encode())
        self.p.stdin.flush()
        if notify:
            return None
        while True:
            line = self.p.stdout.readline()
            if not line:
                raise RuntimeError("server closed stdout")
            resp = json.loads(line)  # any non-JSON garbage on stdout fails here
            if resp.get("id") == self.next_id:
                return resp

    def init(self) -> dict:
        r = self.send("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                     "clientInfo": {"name": "smoke", "version": "0"}})
        self.send("notifications/initialized", notify=True)
        return r

    def close(self) -> None:
        self.p.stdin.close()
        try:
            self.p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.p.kill()


def files_in(text: str, workspace: str) -> list[str]:
    seen = []
    for m in re.finditer(r"([A-Za-z]:)?[\w./\\-]+\.(?:ads|adb|ada)\b", text):
        p = m.group(0)
        ap = p if os.path.isabs(p) else os.path.join(workspace, p)
        if os.path.isfile(ap) and os.path.abspath(ap) not in seen:
            seen.append(os.path.abspath(ap))
    return seen


def find_col(path: str, pattern: str) -> tuple[int, int]:
    with open(path, encoding="latin-1") as f:
        for i, line in enumerate(f, 1):
            m = re.search(pattern, line)
            if m:
                return i, m.start(1) + 1
    raise SystemExit(f"pattern {pattern!r} not found in {path}")


def run_calls(args: list[str], workspace: str, calls: list[tuple[str, dict]], show: bool) -> None:
    w = Wire(args)
    w.init()
    rows = []
    for name, params in calls:
        t = time.time()
        r = w.send("tools/call", {"name": name, "arguments": params})
        dt = time.time() - t
        res = r["result"]
        text = "".join(c.get("text", "") for c in res["content"])
        files = files_in(text, workspace)
        fsize = sum(os.path.getsize(f) for f in files)
        rows.append((name, params, len(text), len(files), fsize, dt, res.get("isError", False)))
        if show:
            print(f"--- {name} {json.dumps(params)}{'  [isError]' if res.get('isError') else ''}\n{text}\n")
    w.close()
    print(f"{'tool':<16} {'args':<58} {'resp':>6} {'files':>5} {'file chars':>10} {'ratio':>6} {'secs':>5}")
    for name, params, n, nf, fsize, dt, err in rows:
        a = json.dumps(params)
        a = a if len(a) <= 58 else a[:55] + "..."
        ratio = f"{fsize / n:.1f}x" if n and fsize else "-"
        print(f"{name:<16} {a:<58} {n:>6} {nf:>5} {fsize:>10} {ratio:>6} {dt:>5.2f}{'  ERR' if err else ''}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--schemas", action="store_true", help="print raw tools/list result and exit")
    ap.add_argument("--gnatcoll", metavar="PATH", help="run against a gnatcoll-core checkout")
    ap.add_argument("--show", action="store_true", help="print every response")
    a = ap.parse_args()

    if a.gnatcoll:
        root = os.path.abspath(a.gnatcoll)
        gnatcoll_os = "windows" if sys.platform == "win32" else "unix"
        args = ["-P", os.path.join(root, "core", "gnatcoll_core.gpr"), "-w", root, f"-XGNATCOLL_OS={gnatcoll_os}"]
        calls = [list(c) for c in GNATCOLL_CALLS]
        # Pick real cursor positions from the sources instead of hardcoding line numbers.
        l, c = find_col(os.path.join(root, "core/src/gnatcoll-json.adb"), r"\b(Write)\s*\(")
        calls[5][1].update(line=l, column=c)
        l, c = find_col(os.path.join(root, "core/src/gnatcoll-json.ads"), r"function (Create_Object)\b")
        calls[6][1].update(line=l, column=c)
        calls = [tuple(c) for c in calls]
        workspace = root
    else:
        args = ["-P", os.path.join(FIXTURE, "fixture.gpr")]
        calls, workspace = FIXTURE_CALLS, FIXTURE

    if a.schemas:
        w = Wire(args)
        init = w.init()
        tools = w.send("tools/list", {})
        w.close()
        print(json.dumps({"instructions": init["result"].get("instructions")}, indent=2))
        print(json.dumps(tools["result"], indent=2))
        return
    run_calls(args, workspace, calls, a.show)


if __name__ == "__main__":
    main()
