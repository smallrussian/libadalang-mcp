"""test_model: the facts needed to test one subprogram, from name resolution.

Sections: signature and parts, contracts, parameters/result with their types'
ranges and boundary values (modular width, enum literals, record components),
branch conditions, raises and handlers, returns, global state read/written,
callees (test-double candidates: foreign imports, dispatching calls, other
units), bit operations (shifts/rotates, and/or/xor on modular types, width
conversions). It does NOT invent expected results: those come from the
specification, computed independently.

The first line is a status for workflow gates: `[status: complete]` or
`[status: INCOMPLETE: ...]` (unresolved names, missing body, parse errors...).
"""

from __future__ import annotations

import os
import re

from lal_mcp.session import Session, ToolFailure, last_line
from lal_mcp.tools.text import one_line

SHIFT_NAMES = {"shift_left", "shift_right", "shift_right_arithmetic", "rotate_left", "rotate_right"}
CONTRACT_ASPECTS = ("Pre", "Post", "Pre'Class", "Post'Class", "Contract_Cases", "Global", "Depends",
                    "Exceptional_Cases")


def _ival(lal, expr):
    try:
        if expr is not None and expr.p_is_static_expr():
            return int(expr.p_eval_as_int)
    except (lal.PropertyError, Exception):
        pass
    return None


def _find_subprogram(sess: Session, file: str, line: int, column: int):
    import libadalang as lal

    from lal_mcp.tools import navigate

    if column > 0:
        loc = navigate._locate(sess, file, line, column)
        dn, approx = navigate.resolve(sess, loc)
        if dn is None:
            raise ToolFailure(approx)
        decl = dn.p_basic_decl
    else:
        path = sess.resolve_path(file)
        unit = sess.unit(path)
        if unit.root is None:
            raise ToolFailure(f"unresolved: {sess.rel(path)} could not be parsed")
        cands = []
        for d in unit.root.findall(lal.BasicDecl):
            try:
                if d.p_is_subprogram and not d.is_a(lal.EnumLiteralDecl)                         and d.p_defining_name.sloc_range.start.line == line:
                    cands.append(d)
            except (lal.PropertyError, Exception):
                continue
        if not cands:
            raise ToolFailure(f"no subprogram declared at {sess.rel(path)}:{line} "
                              "(give the line of a subprogram declaration, e.g. from outline)")
        if len({c.p_defining_name.sloc_range.start.column for c in cands}) > 1:
            names = ", ".join(f"{c.p_defining_name.text} (col {c.p_defining_name.sloc_range.start.column})"
                              for c in cands)
            raise ToolFailure(f"several subprograms on {sess.rel(path)}:{line}: {names}; pass column")
        decl = cands[0]
    if decl.is_a(lal.EnumLiteralDecl):
        raise ToolFailure(f"{decl.p_defining_name.text} is an enumeration literal, not a subprogram")
    if not decl.p_is_subprogram and not decl.is_a(lal.BaseSubpBody):
        raise ToolFailure(f"{decl.p_defining_name.text} is a {decl.kind_name}, not a subprogram")
    return decl


def _parts(lal, decl):
    try:
        parts = list(decl.p_all_parts())
    except (lal.PropertyError, Exception):
        parts = [decl]
    spec = parts[0]
    body = next((p for p in parts if p.is_a(lal.BaseSubpBody) and not p.is_a(lal.SubpBodyStub)), None)
    return spec, body, parts


def describe_type(lal, sess: Session, type_expr, gaps: list[str]) -> tuple[str, list[str]]:
    """(facts, boundary values) for the type of a parameter or result."""
    if type_expr is None:
        return "", []
    try:
        td = type_expr.p_designated_type_decl
    except (lal.PropertyError, Exception) as exc:
        gaps.append(f"type of {one_line(type_expr.text, 40)} unresolved ({last_line(exc)})")
        return "type unresolved", []
    if td is None:
        if type_expr.is_a(lal.AnonymousType) or "access" in type_expr.text.lower():
            return "anonymous access type (null possible unless `not null`)", ["null"]
        gaps.append(f"type of {one_line(type_expr.text, 40)} unresolved")
        return "type unresolved", []
    where = sess.loc(td) if td.unit and td.unit.filename and not td.unit.filename.endswith("__standard") \
        else "Standard"
    facts, bounds = [], []
    if td.is_a(lal.ClasswideTypeDecl) and td.parent is not None and td.parent.is_a(lal.BaseTypeDecl):
        td = td.parent  # T'Class: describe the specific type T
    try:
        if td.p_is_access_type():
            return f"access type ({where}); null is a test case unless `not null`", ["null"]
        if td.p_is_record_type():
            comps = [one_line(c.text, 60) for c in td.findall(lal.ComponentDecl)]
            if comps:
                facts.append("record: " + "; ".join(comps[:8]) + (" ..." if len(comps) > 8 else ""))
            elif "null record" in td.text.lower():
                facts.append("null record (no components)")
            else:
                facts.append("record (components not visible here: private or derived; see the full view)")
            if "tagged" in td.text.lower() or "'class" in type_expr.text.lower():
                facts.append("tagged" + (", abstract" if "abstract" in td.text.lower() else ""))
            if "'class" in type_expr.text.lower():
                facts.append("class-wide: test with each concrete derived type (dispatching)")
        elif td.p_is_char_type() and td.p_defining_name.text.lower() in (
                "character", "wide_character", "wide_wide_character"):
            std = {"character": (256, "Latin-1"), "wide_character": (2**16, "BMP"),
                   "wide_wide_character": (2**31, "UCS-4")}[td.p_defining_name.text.lower()]
            facts.append(f"character type, {std[0]} values ({std[1]})")
            n = td.p_defining_name.text
            bounds = [f"{n}'First (NUL)", "' ' (space)", "'0'", "'A'", f"{n}'Val (127)", f"{n}'Last"]
        elif td.p_is_enum_type():
            base = td.p_base_subtype() if hasattr(td, "p_base_subtype") else td
            lits = [l.p_defining_name.text for l in base.findall(lal.EnumLiteralDecl)]
            if lits:
                facts.append(f"enumeration, {len(lits)} literals: {', '.join(lits[:12])}"
                             + (" ..." if len(lits) > 12 else ""))
                bounds = [lits[0], lits[-1]] if len(lits) > 1 else lits
        elif td.p_is_discrete_type():
            modulus = None
            root = td
            for _ in range(10):  # walk to the type that declares `mod N`
                tdef = getattr(root, "f_type_def", None)
                if tdef is not None and tdef.is_a(lal.ModIntTypeDef):
                    modulus = _ival(lal, tdef.f_expr)
                    break
                try:
                    nxt = root.p_base_subtype()
                except (lal.PropertyError, Exception):
                    nxt = None
                if nxt is None or nxt == root:
                    break
                root = nxt
            lo = hi = None
            try:
                rng = td.p_discrete_range
                lo, hi = _ival(lal, rng.low_bound), _ival(lal, rng.high_bound)
            except (lal.PropertyError, Exception):
                pass
            if modulus is not None:
                # For a modular type Libadalang reports the modulus itself as the upper bound.
                if hi is None or hi >= modulus:
                    hi = modulus - 1
                bits = (modulus - 1).bit_length()
                facts.append(f"modular: mod {modulus} ({bits} bits, wraps around; and/or/xor/not are bitwise)")
                lo = 0 if lo is None else lo
                hi = modulus - 1 if hi is None else hi
            else:
                facts.append("signed integer" if td.p_is_int_type() else "discrete")
            if lo is not None and hi is not None:
                facts.append(f"range {lo} .. {hi}")
                cand = [lo, lo + 1, hi - 1, hi]
                if lo < 0 < hi:
                    cand.append(0)
                if modulus is not None and hi > 2:
                    cand.append((hi + 1) // 2)  # high bit set
                bounds = [str(v) for v in sorted(set(v for v in cand if lo <= v <= hi))]
            else:
                facts.append("range not static")
        elif td.p_is_float_type() or td.p_is_fixed_point():
            facts.append("floating point" if td.p_is_float_type() else "fixed point")
            try:
                rng = td.p_discrete_range
                facts.append(f"range {one_line(rng.low_bound.text, 30)} .. {one_line(rng.high_bound.text, 30)}")
            except (lal.PropertyError, Exception):
                pass
        elif td.p_is_array_type():
            facts.append("array: " + one_line(td.text, 100))
        else:
            facts.append(one_line(td.text, 100))
        size = td.p_get_aspect_spec_expr("Size") if hasattr(td, "p_get_aspect_spec_expr") else None
        if size is not None:
            facts.append(f"Size => {one_line(size.text, 20)}")
    except (lal.PropertyError, Exception) as exc:
        gaps.append(f"type facts for {td.p_defining_name.text}: {last_line(exc)}")
    name = td.p_defining_name.text if td.p_defining_name else "?"
    return f"{name} ({where}): " + "; ".join(facts), bounds


def test_model(sess: Session, file: str, line: int, column: int) -> tuple[str, bool]:
    import libadalang as lal

    decl = _find_subprogram(sess, file, line, column)
    spec, body, parts = _parts(lal, decl)
    gaps: list[str] = []
    try:
        fqn = spec.p_defining_name.p_fully_qualified_name
    except Exception:
        fqn = spec.p_defining_name.text
    sspec = spec.p_subp_spec_or_null()
    kind = sspec.f_subp_kind.text.lower() if sspec is not None else spec.kind_name
    out = []
    part_txt = ", ".join(f"{p.kind_name} {sess.loc(p)}" for p in parts)
    out.append(f"test model: {fqn} [{kind}]  parts: {part_txt}")
    if sspec is not None:
        out.append("signature: " + one_line(sspec.text, 300))

    # --- documentation: the specification to derive expected values from
    doc = ""
    for p in parts:
        try:
            doc = (p.p_doc or "").strip()
        except (lal.PropertyError, Exception):
            doc = ""
        if doc:
            out.append(f"documentation ({sess.loc(p)}), the specification for expected values:")
            text = doc if len(doc) <= 1500 else doc[:1500] + " ..."
            out += [f"  | {ln}" for ln in text.splitlines()]
            break
    if not doc:
        out.append("documentation: none on the declaration; get the specification from the requirements, "
                   "not from the implementation")

    # --- body status
    imported = False
    try:
        imported = spec.p_is_imported
    except (lal.PropertyError, Exception):
        pass
    formal = any(p.is_a(lal.GenericFormalPart) for p in spec.parents(with_self=False))
    if body is None:
        if spec.is_a(lal.GenericSubpInstantiation):
            out.append(f"body: instance of generic {one_line(spec.f_generic_subp_name.text, 60)}: the generic "
                       "provides the body; test the instance's behavior through its actuals")
        elif formal:
            out.append("body: none, generic formal subprogram: supplied by each instantiation")
        elif imported:
            out.append("body: none, imported (foreign code); use link_info for its symbol, test via a double "
                       "or the real library")
        elif spec.is_a(lal.AbstractSubpDecl):
            out.append("body: abstract; test the overriding implementations")
        elif spec.is_a(lal.ExprFunction, lal.NullSubpDecl):
            out.append("body: " + one_line(spec.text, 200))
        else:
            out.append("body: MISSING in the project")
            gaps.append("missing body")

    # --- contracts
    contracts = []
    for p in parts:
        for asp in CONTRACT_ASPECTS:
            try:
                e = p.p_get_aspect_spec_expr(asp)
            except (lal.PropertyError, Exception):
                e = None
            if e is not None:
                contracts.append(f"  {asp} => {one_line(e.text, 200)}  ({sess.loc(e)})")
    out.append("contracts:" + ("" if contracts else " none declared"))
    out += contracts

    # --- parameters and result
    out.append("parameters:" + ("" if sspec is not None and sspec.f_subp_params else " none"))
    if sspec is not None and sspec.f_subp_params:
        for ps in sspec.f_subp_params.f_params:
            mode = ps.f_mode.text or "in"
            names = ", ".join(i.text for i in ps.f_ids)
            default = f" := {one_line(ps.f_default_expr.text, 40)}" if ps.f_default_expr else ""
            facts, bounds = describe_type(lal, sess, ps.f_type_expr, gaps)
            out.append(f"  {names} : {mode} {one_line(ps.f_type_expr.text, 60)}{default}")
            if facts:
                out.append(f"      type {facts}")
            if bounds:
                out.append(f"      boundary values: {', '.join(bounds)}")
            if mode in ("out", "in out"):
                out.append("      written by the subprogram: check its value after the call")
    if sspec is not None and sspec.f_subp_returns is not None:
        facts, bounds = describe_type(lal, sess, sspec.f_subp_returns, gaps)
        out.append(f"result: {one_line(sspec.f_subp_returns.text, 60)}")
        if facts:
            out.append(f"      type {facts}")

    gt = gnattest_test(sess, spec)
    if gt:
        out.append(gt)

    if body is not None:
        out += _body_analysis(lal, sess, body, spec, gaps)

    for p in parts:
        if p.unit is not None and sess.has_errors(p.unit):
            gaps.append(f"parse errors in {sess.rel(p.unit.filename)}")
    gaps = list(dict.fromkeys(gaps))
    status = "[status: complete]" if not gaps else "[status: INCOMPLETE: " + "; ".join(gaps[:8]) + "]"
    note = ("expected results: derive from the specification/contract and compute them independently; "
            "this model lists inputs, paths and effects, not oracle values")
    return "\n".join([status] + out + [note]), False


def gnattest_test(sess: Session, spec) -> str | None:
    """The GNATtest-generated test for this subprogram, found by its marker comment
    `--  <spec file>:<line>:<col>:<Name>`, when a GNATtest harness is loaded."""
    if not getattr(sess, "harness", None):
        return None
    sl = spec.sloc_range.start
    try:
        name = spec.p_defining_name.text.split(".")[-1]
    except Exception:
        return None
    marker = f"--  {os.path.basename(spec.unit.filename)}:{sl.line}:{sl.column}:{name}".lower()
    for path in sess.sources():
        if not path.lower().endswith("-tests.adb"):
            continue
        try:
            with open(path, encoding="latin-1") as f:
                lines = f.read().splitlines()
        except OSError:
            continue
        for i, ln in enumerate(lines):
            if ln.strip().lower() != marker:
                continue
            proc_line, proc = i + 1, "?"
            for j in range(i, max(-1, i - 6), -1):  # the `procedure Test_X (...) is` just above
                m = re.match(r"\s*procedure\s+(\w+)", lines[j], re.I)
                if m:
                    proc_line, proc = j + 1, m.group(1)
                    break
            end = next((k for k in range(i, len(lines)) if re.match(rf"\s*end\s+{re.escape(proc)}\s*;", lines[k], re.I)),
                       len(lines) - 1)
            skeleton = any("test not implemented" in lines[k].lower() for k in range(i, end + 1))
            state = ("generated skeleton, not implemented yet: write the test between the `--  end read only` "
                     "and `--  begin read only` markers" if skeleton else "implemented")
            out = f"gnattest test: {sess.rel(path)}:{proc_line} {proc} ({state})"
            if sess.harness_mode == "separate":
                out += "\n" + _driver_line(sess, spec, sl)
            return out
    return ("gnattest test: none generated for this subprogram in the loaded harness "
            f"(regenerate with gnattest -P {sess.rel(sess.project_path)}; existing tests are kept)")


def _driver_line(sess: Session, spec, sl) -> str:
    """The --separate-drivers driver that registers this subprogram's test (by its spec position)."""
    hits = [d for d in sess.harness_drivers() if d.runs(spec.unit.filename, sl.line, sl.column)]
    if not hits:
        return ("gnattest driver: none of the harness drivers registers this test "
                "(regenerate the harness with the same gnattest switches)")
    parts = []
    for d in hits[:3]:
        runner = sess.rel(d.runner) if d.runner else "unknown"
        built = "" if d.runner and os.path.isfile(d.runner) else " (not built yet)"
        parts.append(f"gnattest driver: {sess.rel(d.project)}; runner {runner}{built}")
    return "\n".join(parts)


def _body_analysis(lal, sess: Session, body, spec, gaps: list[str]) -> list[str]:
    out = []
    own = {body, spec}
    stmts = body

    # branches
    br = []
    for n in stmts.findall(lambda x: x.is_a(lal.IfStmt, lal.ElsifStmtPart, lal.CaseStmt, lal.WhileLoopSpec,
                                               lal.ForLoopSpec, lal.ExitStmt, lal.IfExpr, lal.CaseExpr,
                                               lal.QuantifiedExpr)):
        if n.is_a(lal.IfStmt):
            br.append((n, f"if {one_line(n.f_cond_expr.text, 120)}"))
        elif n.is_a(lal.ElsifStmtPart):
            br.append((n, f"elsif {one_line(n.f_cond_expr.text, 120)}"))
        elif n.is_a(lal.CaseStmt):
            alts = [one_line(a.f_choices.text, 40) for a in n.f_alternatives]
            br.append((n, f"case {one_line(n.f_expr.text, 60)}: when {' | '.join(alts)}"))
        elif n.is_a(lal.WhileLoopSpec):
            br.append((n, f"while {one_line(n.f_expr.text, 120)}"))
        elif n.is_a(lal.ForLoopSpec):
            br.append((n, f"for {one_line(n.text, 120)} (0, 1 and many iterations)"))
        elif n.is_a(lal.ExitStmt) and n.f_cond_expr is not None:
            br.append((n, f"exit when {one_line(n.f_cond_expr.text, 120)}"))
        elif n.is_a(lal.IfExpr):
            br.append((n, f"if-expression ({one_line(n.f_cond_expr.text, 100)})"))
        elif n.is_a(lal.CaseExpr):
            br.append((n, f"case-expression on {one_line(n.f_expr.text, 60)}"))
        elif n.is_a(lal.QuantifiedExpr):
            br.append((n, f"quantified: {one_line(n.text, 100)}"))
    for n in stmts.findall(lambda x: x.is_a(lal.BinOp) and x.f_op.is_a(lal.OpAndThen, lal.OpOrElse)):
        br.append((n, f"short-circuit: {one_line(n.text, 120)}"))
    out.append(f"branches ({len(br)}):" + ("" if br else " none (straight-line code)"))
    out += [f"  {sess.loc(n)}  {t}" for n, t in sorted(br, key=lambda t: t[0].sloc_range.start.line)]

    # raises and handlers
    rs = []
    for n in stmts.findall(lambda x: x.is_a(lal.RaiseStmt, lal.RaiseExpr)):
        rs.append(f"  {sess.loc(n)}  {one_line(n.text, 140)}")
    for h in stmts.findall(lal.ExceptionHandler):
        rs.append(f"  {sess.loc(h)}  handler: when {one_line(h.f_handled_exceptions.text, 80)}")
    out.append("raises/handlers:" + ("" if rs else " none explicit (implicit Constraint_Error from range checks "
                                                    "still applies)"))
    out += rs

    # returns
    rets = [f"  {sess.loc(n)}  {one_line(n.text, 140)}"
            for n in stmts.findall(lambda x: x.is_a(lal.ReturnStmt, lal.ExtendedReturnStmt))]
    if rets:
        out.append(f"returns ({len(rets)}):")
        out += rets

    # global state, calls, bit operations, unresolved names
    reads, writes, calls, bitops, unresolved = {}, {}, {}, [], []
    for name in stmts.findall(lal.BaseId):
        p = name.parent
        if p is not None and (p.is_a(lal.DefiningName, lal.EndName) or
                              (p.is_a(lal.AttributeRef) and p.f_attribute == name) or
                              (p.is_a(lal.AspectAssoc) and p.f_id == name) or
                              (p.is_a(lal.PragmaNode))):
            continue
        if name.is_a(lal.StringLiteral, lal.CharLiteral):
            continue
        try:
            dn = name.p_referenced_defining_name()
        except lal.PropertyError:
            dn = None
        if dn is None:
            if any(a.is_a(lal.PragmaNode) for a in name.parents(with_self=False)):
                continue  # pragma arguments such as C in pragma Import (C, ...) or All_Checks
            asp = next((a for a in name.parents(with_self=False) if a.is_a(lal.AspectAssoc)), None)
            if asp is not None and asp.f_id.text.lower() in ("convention", "import", "export", "external_name",
                                                              "link_name"):
                continue  # convention identifiers (Ada, C, Stdcall...) are not references
            if not name.is_a(lal.Op):
                unresolved.append(f"{name.text} at {sess.rel(name.unit.filename)}:{name.sloc_range.start.line}")
            continue
        d = dn.p_basic_decl
        if d.is_a(lal.ObjectDecl):
            inside = any(a in own for a in d.parents(with_self=False))
            if not inside and not d.is_a(lal.ParamSpec):
                try:
                    w = name.p_is_write_reference()
                except lal.PropertyError:
                    w = False
                key = dn.p_fully_qualified_name if hasattr(dn, "p_fully_qualified_name") else dn.text
                (writes if w else reads).setdefault(key, sess.loc(d))
        elif d.is_a(lal.EnumLiteralDecl):
            continue  # literals such as False are not calls
        elif d.p_is_subprogram and name.p_is_call:
            outer = name
            while outer.parent is not None and outer.parent.is_a(lal.DottedName) and outer.parent.f_suffix == outer:
                outer = outer.parent
            try:
                imported = d.p_is_imported
            except lal.PropertyError:
                imported = False
            try:
                disp = outer.p_is_dispatching_call()
            except (lal.PropertyError, Exception):
                disp = False
            try:
                q = dn.p_fully_qualified_name
            except Exception:
                q = dn.text
            simple = dn.text.split(".")[-1].lower()
            if simple in SHIFT_NAMES:
                bitops.append(f"  {sess.loc(name)}  {q} (shift/rotate: direction {simple.split('_')[0]}, "
                              f"check amount >= width and the operand's bit width)")
                continue
            tags = []
            if imported:
                tags.append("foreign import (link_info: symbol and owner)")
            if disp:
                tags.append("dispatching (test each overriding)")
            unit_of = d.unit.filename if d.unit else ""
            if unit_of and not sess.in_project(unit_of):
                tags.append("runtime/library")
            elif d.unit != spec.unit and d.unit != body.unit:
                tags.append("other unit")
            calls.setdefault(q, (sess.loc(name), tags))
    for op in stmts.findall(lambda x: x.is_a(lal.BinOp, lal.UnOp)):
        o = op.f_op
        if not o.is_a(lal.OpAnd, lal.OpOr, lal.OpXor, lal.OpNot, lal.OpPow, lal.OpMod, lal.OpRem):
            continue
        try:
            t = op.p_expression_type
        except (lal.PropertyError, Exception):
            t = None
        if t is None:
            continue
        try:
            mod_def = any(x.is_a(lal.ModIntTypeDef) for x in (t, *t.p_base_types) if x is not None
                          for x in [getattr(x, "f_type_def", None)] if x is not None)
        except (lal.PropertyError, Exception):
            mod_def = False
        if mod_def or (o.is_a(lal.OpMod, lal.OpRem) and not t.p_defining_name.text.lower().startswith("root_")):
            bitops.append(f"  {sess.loc(op)}  {one_line(op.text, 80)}  (type {t.p_defining_name.text}"
                          f"{', modular' if mod_def else ''})")
    for conv in stmts.findall(lal.CallExpr):
        try:
            tgt = conv.f_name.p_referenced_decl()
        except (lal.PropertyError, Exception):
            tgt = None
        if tgt is not None and tgt.is_a(lal.BaseTypeDecl) and conv.f_suffix is not None:
            try:
                src_t = conv.f_suffix[0].f_r_expr.p_expression_type if len(conv.f_suffix) else None
            except (lal.PropertyError, Exception):
                src_t = None
            src_name = src_t.p_defining_name.text.lower() if src_t is not None and src_t.p_defining_name else ""
            if src_name.startswith(("root_", "universal")):
                continue  # literal/static expression: no real width conversion
            if src_t is not None and src_t.p_is_discrete_type() and tgt.p_is_discrete_type():
                bitops.append(f"  {sess.loc(conv)}  conversion {src_t.p_defining_name.text} -> "
                              f"{tgt.p_defining_name.text}: {one_line(conv.text, 70)} (range/width check)")

    out.append("global state:" + ("" if (reads or writes) else " none (pure with respect to package state)"))
    out += [f"  writes {k} ({v})" for k, v in sorted(writes.items())]
    out += [f"  reads  {k} ({v})" for k, v in sorted(reads.items()) if k not in writes]
    out.append("calls (test-double candidates):" + ("" if calls else " none"))
    out += [f"  {loc}  {q}" + (f"  [{', '.join(tags)}]" if tags else "") for q, (loc, tags) in sorted(calls.items())]
    if bitops:
        out.append("bit/width operations:")
        out += sorted(set(bitops))
    if unresolved:
        uniq = list(dict.fromkeys(unresolved))
        out.append("unresolved names: " + ", ".join(uniq[:10]) + (" ..." if len(uniq) > 10 else ""))
        gaps.append(f"{len(uniq)} unresolved names in the body")
    return out
