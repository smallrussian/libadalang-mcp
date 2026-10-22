"""Measure lal-mcp's name resolution against the GNAT compiler's cross-references.

Ground truth: the X (cross-reference) sections of the .ali files GNAT writes when
compiling with -gnatc. For every reference GNAT recorded in a project source,
we run the same code path as the goto_definition tool and check whether we land
on the declaration GNAT says. For a sample of entities we also compare the
find_references result with GNAT's list of references.

    gprbuild -P prj.gpr -c -k -gnatc --relocate-build-tree=/tmp/ali ...   # once
    python scripts/eval_xref.py -P prj.gpr -w <workspace> --ali-dir /tmp/ali [-X VAR=VAL ...]

Only `r` (read), `m` (modify), `s` (call statement) and `w` (with clause)
references are compared. Ends of declarations, body/completion markers, implicit
references and dispatching info are skipped.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import random
import re
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lal_mcp.session import Config, Session, ToolFailure  # noqa: E402
from lal_mcp.tools import navigate  # noqa: E402

REF_KINDS = set("rmsw")
# entity: line, type char, col, level ('*' or ' '), name (identifier or "op" or 'c')
ENTITY_RE = re.compile(r'^(\d+)(.)(\d+)([* ])("[^"]*"|\'.\'|[A-Za-z_][\w]*)')
# a reference: optional "file|", line, type char, col
REF_RE = re.compile(r'^(?:(\d+)\|)?(\d+)(.)(\d+)')


def parse_ali(path: str):
    """Yield (entity_name, def_loc, [(ref_loc, kind)]) with locs = (file_basename, line, col)."""
    deps: list[str] = []
    entities = []
    cur_file = None
    cur = None
    with open(path, encoding="latin-1") as f:
        lines = f.read().splitlines()
    for ln in lines:
        if ln.startswith("D "):
            deps.append(ln.split()[1])
        elif ln.startswith("X "):
            parts = ln.split()
            cur_file = deps[int(parts[1]) - 1] if int(parts[1]) <= len(deps) else parts[2]
            cur = None
        elif cur_file and ln and (ln[0].isdigit() or ln.startswith(". ")):
            if ln[0].isdigit():
                m = ENTITY_RE.match(ln)
                if not m:
                    cur = None
                    continue
                name = m.group(5)
                cur = {"name": name, "def": (cur_file, int(m.group(1)), int(m.group(3))),
                       "kind": m.group(2), "refs": [], "file": cur_file}
                entities.append(cur)
                rest = ln[m.end():]
            else:
                if cur is None:
                    continue
                rest = ln[2:]
            # strip bracketed annotations that may contain spaces ({...} <...> (...) [...])
            rest = re.sub(r"\[[^\]]*\]|\{[^}]*\}|<[^>]*>|\([^)]*\)", "", rest)
            for tok in rest.split():
                m = REF_RE.match(tok)
                if not m:
                    continue
                if m.group(1):
                    n = int(m.group(1))
                    cur["file"] = deps[n - 1] if n <= len(deps) else cur["file"]
                cur["refs"].append(((cur["file"], int(m.group(2)), int(m.group(4))), m.group(3)))
    return deps, entities


def dn_loc(dn) -> tuple[str, int, int]:
    """(basename, line, col) of the identifier GNAT uses for an entity: the last part of a dotted name."""
    import libadalang as lal

    name = dn.f_name
    if name.is_a(lal.DottedName):
        name = name.f_suffix
    sl = name.sloc_range.start
    col = sl.column + (1 if name.text.startswith('"') else 0)  # GNAT points inside "op" quotes
    return (os.path.basename(dn.unit.filename).lower(), sl.line, col)


def _parts(dn) -> list:
    try:
        return list(dn.p_all_parts())
    except Exception:  # noqa: BLE001
        return [dn]


def covered_files(alis: list[str]) -> set[str]:
    """Source files GNAT compiled (U lines): only references there have ground truth."""
    files = set()
    for ali in alis:
        with open(ali, encoding="latin-1") as f:
            for ln in f:
                if ln.startswith("U "):
                    files.add(ln.split()[2].lower())
    return files


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("-P", "--project", required=True)
    ap.add_argument("-w", "--workspace")
    ap.add_argument("-X", dest="scenario", action="append", default=[])
    ap.add_argument("--ali-dir", required=True)
    ap.add_argument("--entities", type=int, default=300, help="entities sampled for find_references")
    ap.add_argument("--json", help="write the summary as JSON here")
    ap.add_argument("--seed", type=int, default=1)
    a = ap.parse_args()

    sess = Session(Config(project=a.project, workspace=a.workspace,
                          scenario=dict(x.split("=", 1) for x in a.scenario)))
    t0 = time.monotonic()
    sess.ensure_loaded()
    load_s = time.monotonic() - t0
    by_base = {}
    for f in sess.sources():
        by_base.setdefault(os.path.basename(f).lower(), f)

    alis = [os.path.join(r, f) for r, _, fs in os.walk(a.ali_dir) for f in fs if f.endswith(".ali")]
    covered = covered_files(alis)
    refs: dict[tuple, tuple] = {}  # ref_loc -> (def_loc, entity name)
    # One entity appears in the .ali of every unit that references it: merge them.
    merged: dict[tuple, tuple[str, set]] = {}
    for ali in alis:
        _, ents = parse_ali(ali)
        for e in ents:
            d = (e["def"][0].lower(), e["def"][1], e["def"][2])
            for (rf, rl, rc), kind in e["refs"]:
                if rf.lower() in by_base:
                    covered.add(rf.lower())  # includes separate bodies (no U line of their own)
                if kind not in REF_KINDS or rf.lower() not in by_base:
                    continue
                loc = (rf.lower(), rl, rc)
                refs[loc] = (d, e["name"])
                if d[0] in by_base:
                    merged.setdefault(d, (e["name"], set()))[1].add(loc)
    entities_all = sorted((name, d, sorted(locs)) for d, (name, locs) in merged.items())

    # ---------------------------------------------------- goto_definition
    outcome = collections.Counter()
    samples = collections.defaultdict(list)
    times = []
    for loc, (gdef, gname) in sorted(refs.items()):
        path = by_base[loc[0]]
        t = time.monotonic()
        try:
            located = navigate._locate(sess, path, loc[1], loc[2])
            dn, approx = navigate.resolve(sess, located)
            if dn is None:
                kind = "no_declaration" if approx.startswith("no declaration") else "unresolved"
            else:
                mine = dn_loc(dn)
                if mine == gdef:
                    kind = "exact"
                elif gdef in {dn_loc(p) for p in _parts(dn)}:
                    kind = "same_entity_other_part"  # e.g. full view of a private type, body's copy of a parameter
                else:
                    kind = "wrong"
                if approx:
                    kind += "_approx"
        except ToolFailure as exc:
            kind, mine = ("not_on_a_name" if "no name at" in str(exc) else "tool_error"), None
        except Exception as exc:  # noqa: BLE001
            kind, mine = "crash:" + type(exc).__name__, None
        times.append(time.monotonic() - t)
        outcome[kind] += 1
        if kind != "exact" and len(samples[kind]) < 8:
            samples[kind].append(f"{loc[0]}:{loc[1]}:{loc[2]} {gname}: gnat -> {gdef}"
                                 + (f", lal -> {mine}" if kind.startswith(("wrong", "same")) else ""))

    total = sum(outcome.values())
    exact = outcome["exact"] + outcome["exact_approx"]
    same = outcome["same_entity_other_part"] + outcome["same_entity_other_part_approx"]
    wrong = outcome["wrong"] + outcome["wrong_approx"]

    # ---------------------------------------------------- find_references
    import libadalang as lal

    random.seed(a.seed)
    pick = random.sample(entities_all, min(a.entities, len(entities_all)))
    units = sess.all_units()
    tp = fp = fn = 0
    ref_times = []
    ref_samples = []
    for name, gdef, grefs in pick:
        unit = sess.unit(by_base[gdef[0]])
        node = unit.root.lookup(lal.Sloc(gdef[1], gdef[2]))
        dn = node
        while dn is not None and not dn.is_a(lal.DefiningName):
            dn = dn.parent
        if dn is None:
            fn += len(set(grefs))
            continue
        t = time.monotonic()
        try:
            found = set()
            for r in dn.p_find_all_references(units):
                if str(r.kind).endswith("no_ref"):
                    continue
                n = r.ref
                if navigate.is_decl_or_end_name(n):
                    continue  # same filter as the find_references tool
                base = os.path.basename(n.unit.filename).lower()
                if base not in covered:
                    continue  # no GNAT ground truth for this file
                sl = n.sloc_range.start
                found.add((base, sl.line, sl.column))
        except lal.PropertyError:
            found = set()
        ref_times.append(time.monotonic() - t)
        g = set(grefs)
        tp += len(g & found)
        missed, extra = g - found, found - g
        fn += len(missed)
        fp += len(extra)
        if (missed or extra) and len(ref_samples) < 10:
            ref_samples.append(f"{name} @ {gdef}: missed {sorted(missed)[:3]}, extra {sorted(extra)[:3]}")

    def pct(x, n):
        return round(100.0 * x / n, 2) if n else None

    def ms(xs, q):
        return round(1000 * statistics.quantiles(xs, n=100)[q - 1], 2) if len(xs) > 2 else None

    summary = {
        "project": os.path.abspath(a.project),
        "sources": len(sess.sources()),
        "ali_files": len(alis),
        "files_with_ground_truth": len(covered),
        "project_load_s": round(load_s, 2),
        "goto_definition": {
            "gnat_references": total,
            "exact_pct": pct(exact, total),
            "same_entity_pct": pct(exact + same, total),
            "wrong_entity_pct": pct(wrong, total),
            "no_answer_pct": pct(total - exact - same - wrong, total),
            "outcomes": dict(outcome.most_common()),
            "p50_ms": ms(times, 50), "p95_ms": ms(times, 95),
        },
        "find_references": {
            "entities_sampled": len(pick),
            "recall_pct": pct(tp, tp + fn), "precision_pct": pct(tp, tp + fp),
            "true_pos": tp, "missed": fn, "extra": fp,
            "p50_ms": ms(ref_times, 50), "p95_ms": ms(ref_times, 95),
        },
        "samples": {k: v for k, v in samples.items()},
        "find_references_samples": ref_samples,
    }
    print(json.dumps(summary, indent=2))
    if a.json:
        with open(a.json, "w") as f:
            json.dump(summary, f, indent=2)


if __name__ == "__main__":
    main()
