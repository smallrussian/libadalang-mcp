"""Generate agent questions with ground truth from GNAT cross-references (.ali).

Questions are sampled with a fixed seed so they are not hand-picked for the
tools. Two kinds, both about overloaded subprograms (where name matching alone
is not enough):

  resolve_call:  "Which declaration does the call to X at <file>:<line> call?"
                 expected: the declaration GNAT resolved it to.
  overload_callers: "List every call site of the X declared at <file>:<line>."
                 expected: GNAT's references to exactly that overload.

    python scripts/make_agent_questions.py --ali-dir /tmp/ali --src <repo> \
        --out scripts/agent_questions_generated.json [--resolve 6 --callers 4 --seed 7]
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import random
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from eval_xref import parse_ali  # noqa: E402

SUBPROGRAM_KINDS = set("UV")  # procedure, function


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ali-dir", required=True)
    ap.add_argument("--src", required=True, help="repository root (questions use paths relative to it)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--resolve", type=int, default=6)
    ap.add_argument("--callers", type=int, default=4)
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()

    rel = {}
    for root, _, files in os.walk(a.src):
        if "testsuite" in root.split(os.sep) or "/obj" in root.replace("\\", "/"):
            continue
        for f in files:
            if f.endswith((".ads", ".adb")):
                rel.setdefault(f.lower(), os.path.relpath(os.path.join(root, f), a.src).replace("\\", "/"))
    text_cache: dict[str, list[str]] = {}

    def line_of(base: str, n: int) -> str:
        if base not in text_cache:
            with open(os.path.join(a.src, rel[base]), encoding="latin-1") as f:
                text_cache[base] = f.read().splitlines()
        lines = text_cache[base]
        return lines[n - 1] if 0 < n <= len(lines) else ""

    # entity (decl file, line, col) -> name, kind, refs {(file, line, col, kind)}
    ents: dict[tuple, dict] = {}
    for root, _, files in os.walk(a.ali_dir):
        for f in files:
            if not f.endswith(".ali"):
                continue
            _, parsed = parse_ali(os.path.join(root, f))
            for e in parsed:
                key = (e["def"][0].lower(), e["def"][1], e["def"][2])
                ent = ents.setdefault(key, {"name": e["name"], "kind": e["kind"], "refs": set()})
                for (rf, rl, rc), k in e["refs"]:
                    ent["refs"].add((rf.lower(), rl, rc, k))

    def decl_text(base: str, n: int) -> str:
        """The declaration from its first line to the first ';' outside parentheses (at most 8 lines)."""
        text = " ".join(" ".join(line_of(base, i).split("--")[0] for i in range(n, n + 8)).split())
        depth = 0
        for i, ch in enumerate(text):
            depth += ch == "("
            depth -= ch == ")"
            if ch == ";" and depth == 0:
                return text[: i + 1]
        return text

    subps = {k: v for k, v in ents.items() if v["kind"] in SUBPROGRAM_KINDS and k[0] in rel
             and not line_of(k[0], k[1]).lstrip().lower().startswith("with ")}  # skip generic formals
    by_name = collections.defaultdict(list)
    for k, v in subps.items():
        by_name[(k[0], v["name"].lower())].append(k)
    overloaded = {k for group in by_name.values() if len(group) >= 3 for k in group}

    rng = random.Random(a.seed)
    questions = []

    # resolve_call: a call in another file, the name appears once on that line
    candidates = []
    for k in sorted(overloaded):
        v = subps[k]
        for rf, rl, rc, kind in sorted(v["refs"]):
            if kind not in "rs" or rf == k[0] or rf not in rel:
                continue
            ln = line_of(rf, rl)
            if len(re.findall(r"\b%s\b" % re.escape(v["name"]), ln, re.I)) != 1:
                continue
            candidates.append((k, rf, rl, ln.strip()))
    seen_decl = set()
    rng.shuffle(candidates)
    for k, rf, rl, ln in candidates:
        if len([q for q in questions if q["kind"] == "resolve_call"]) >= a.resolve:
            break
        if k in seen_decl:
            continue
        seen_decl.add(k)
        name = subps[k]["name"]
        n_over = len(by_name[(k[0], name.lower())])
        questions.append({
            "id": f"resolve_{name}_{rf.split('.')[0]}_{rl}",
            "kind": "resolve_call",
            "question": (f"In {rel[rf]} at line {rl} ({ln}) there is a call to {name}. "
                         f"{name} is overloaded ({n_over} declarations in {rel[k[0]]}). Which declaration does this "
                         f"call resolve to? Give the declaration's location and its full signature."),
            "expected": [f"{k[0]}:{k[1]}"],
            # spec or body of the right subprogram both count (GNAT 'b' = body location)
            "expected_groups": [[f"{k[0]}:{k[1]}"] + sorted(f"{rf}:{rl}" for rf, rl, rc, kind in subps[k]["refs"]
                                                             if kind == "b")],
            "allow": [],
            "fp_files": sorted({k[0]} | {rf for s in by_name[(k[0], name.lower())]
                                         for rf, rl, rc, kind in subps[s]["refs"] if kind == "b"}),
        })

    # overload_callers: 2-10 references outside the declaring file
    cands = []
    for k in sorted(overloaded):
        v = subps[k]
        calls = {(rf, rl) for rf, rl, rc, kind in v["refs"] if kind in "rs" and rf in rel and rf != k[0]}
        if 2 <= len(calls) <= 10:
            cands.append((k, calls))
    rng.shuffle(cands)
    used_names = set()
    for k, calls in cands:
        if len([q for q in questions if q["kind"] == "overload_callers"]) >= a.callers:
            break
        name = subps[k]["name"]
        if name.lower() in used_names:
            continue
        used_names.add(name.lower())
        siblings = by_name[(k[0], name.lower())]
        fp_files = sorted({rf for s in siblings for rf, *_ in subps[s]["refs"] if rf in rel and rf != k[0]})
        questions.append({
            "id": f"callers_{name}_{k[1]}",
            "kind": "overload_callers",
            "question": (f"{rel[k[0]]} declares {len(siblings)} overloads of {name}. List every call site, "
                         f"anywhere in the project's sources, of the one declared at {rel[k[0]]}:{k[1]} "
                         f"({decl_text(k[0], k[1])}). Do not include calls to the other overloads."),
            "expected": sorted(f"{rf}:{rl}" for rf, rl in calls),
            "allow": [],
            "fp_files": fp_files,
        })

    with open(a.out, "w", encoding="utf-8") as f:
        json.dump({"_comment": f"generated by make_agent_questions.py --seed {a.seed}; ground truth from GNAT .ali",
                   "questions": questions}, f, indent=2)
    for q in questions:
        print(q["id"], "| expected:", q["expected"])
        print("   ", q["question"][:220])


if __name__ == "__main__":
    main()
