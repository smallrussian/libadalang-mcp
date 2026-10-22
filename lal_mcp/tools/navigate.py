"""goto_definition and find_references."""

from __future__ import annotations

from dataclasses import dataclass

from lal_mcp.session import Session, ToolFailure, last_line
from lal_mcp.tools.text import capped_lines, dedent_block, one_line


@dataclass
class Located:
    unit: object
    path: str
    id_node: object  # the BaseId token under the cursor
    full_name: str  # dotted name around it, for messages
    where: str  # path:line:col


def _locate(sess: Session, file: str, line: int, column: int) -> Located:
    import libadalang as lal

    path = sess.resolve_path(file)
    unit = sess.unit(path)
    n_lines = unit.text.count("\n") + 1
    where = f"{sess.rel(path)}:{line}:{column}"
    if line < 1 or line > n_lines:
        raise ToolFailure(f"line {line} out of range: {sess.rel(path)} has {n_lines} lines")
    if column < 1:
        raise ToolFailure("column is 1-based")
    if unit.root is None:
        raise ToolFailure(f"unresolved: {sess.rel(path)} could not be parsed")
    node = unit.root.lookup(lal.Sloc(line, column))
    if node is None or not node.is_a(lal.BaseId):
        ids = [i for i in unit.root.findall(lal.BaseId) if i.sloc_range.start.line == line]
        names = ", ".join(f"{i.text} (col {i.sloc_range.start.column})" for i in ids[:15])
        raise ToolFailure(
            f"no name at {where}. " + (f"Names on line {line}: {names}" if names else f"Line {line} has no names.")
        )
    full = node
    while full.parent is not None and full.parent.is_a(lal.DottedName) and full.parent.f_suffix == full:
        full = full.parent
    return Located(unit, path, node, full.text, where)


def _defining_name_of(loc: Located):
    """If the cursor is on a declaration's own name, return that DefiningName."""
    import libadalang as lal

    n = loc.id_node
    p = n.parent
    if p is not None and p.is_a(lal.DottedName) and p.f_suffix == n:
        n, p = p, p.parent
    if p is not None and p.is_a(lal.DefiningName) and p.f_name == n:
        return p
    return None


def resolve(sess: Session, loc: Located):
    """(DefiningName, approx_reason|None) or (None, reason). Never guesses silently."""
    import libadalang as lal

    dn = _defining_name_of(loc)
    approx = None
    if dn is None:
        err = None
        try:
            dn = loc.id_node.p_referenced_defining_name()
        except lal.PropertyError as exc:
            err = last_line(exc)
        if dn is None:
            try:
                dn = loc.id_node.p_referenced_defining_name(imprecise_fallback=True)
            except lal.PropertyError as exc:
                err = err or last_line(exc)
            if dn is not None:
                approx = "imprecise fallback"
        if dn is None:
            why = _no_declaration_reason(lal, loc.id_node)
            if why and not err:
                return None, f"no declaration: {loc.full_name} at {loc.where} is {why}"
            return None, f"unresolved: {loc.full_name} at {loc.where}" + (f" ({err})" if err else "")
    if sess.has_errors(loc.unit):
        approx = (approx + "; " if approx else "") + "file has parse errors"
    return dn, approx


def _no_declaration_reason(lal, node) -> str | None:
    """Names that legitimately have no source declaration to go to."""
    p = node.parent
    if p is not None and p.is_a(lal.AttributeRef) and p.f_attribute == node:
        return "a language-defined attribute"
    if p is not None and p.is_a(lal.PragmaNode) and p.f_id == node:
        return "a pragma name"
    if p is not None and p.is_a(lal.AspectAssoc) and p.f_id == node:
        return "an aspect name"
    if node.is_a(lal.Op):
        return "a predefined operator (no user-defined overload matched)"
    if node.is_a(lal.StringLiteral):
        return "a string literal"
    if node.is_a(lal.CharLiteral):
        return "a character literal"
    return None


def _part_label(lal, part) -> str:
    if part.is_a(lal.BodyStub):
        return "stub (is separate)"
    if part.is_a(lal.BodyNode):
        return "body"
    if part.is_a(lal.IncompleteTypeDecl):
        return "incomplete view"
    return "decl"


def _decl_text(lal, decl) -> str:
    col = decl.sloc_range.start.column
    if decl.is_a(lal.BaseSubpBody):
        spec = decl.p_subp_spec_or_null()
        return dedent_block(spec.text, col) + " is ..." if spec is not None else one_line(decl.text)
    if decl.is_a(lal.PackageDecl, lal.PackageBody, lal.GenericPackageDecl, lal.TaskBody, lal.ProtectedBody):
        n = decl.text.count("\n") + 1
        return f"{decl.text.splitlines()[0]} ... ({n} lines; use outline)"
    return capped_lines(dedent_block(decl.text, col))


def _qualified(dn) -> str:
    """Fully qualified name of a DefiningName (works for `A, B : T` declarations too)."""
    try:
        return dn.p_fully_qualified_name
    except Exception:
        return dn.text


def goto_definition(sess: Session, file: str, line: int, column: int) -> tuple[str, bool]:
    import libadalang as lal

    loc = _locate(sess, file, line, column)
    dn, approx = resolve(sess, loc)
    if dn is None:
        return approx, False
    decl = dn.p_basic_decl
    out = []
    if approx:
        out.append(f"[approximate: {approx}]")
    out.append(f"{_qualified(dn)}  [{decl.kind_name}]")
    if decl.unit.filename.endswith("__standard") or not decl.unit.filename:
        out.append("predefined in package Standard")
        out.append(one_line(decl.text))
        return "\n".join(out), False
    out.append(f"{sess.loc(decl)}")
    out.append(_decl_text(lal, decl))
    try:
        parts = list(decl.p_all_parts())
    except Exception:  # PropertyError / precondition failures on multi-name decls
        parts = [decl]
    others = [p for p in parts if p != decl]
    if others:
        out.append("other parts:")
        for p in others:
            out.append(f"  {_part_label(lal, p):<18} {sess.loc(p)}")
    return "\n".join(out), False


def find_references(sess: Session, file: str, line: int, column: int, limit: int) -> tuple[str, bool]:
    import libadalang as lal

    if limit < 1:
        raise ToolFailure("limit must be >= 1")
    loc = _locate(sess, file, line, column)
    dn, approx = resolve(sess, loc)
    if dn is None:
        return approx, False
    decl = dn.p_basic_decl
    units = sess.all_units()
    try:
        refs = list(dn.p_find_all_references(units))
    except lal.PropertyError as exc:
        return f"error: reference search failed for {_qualified(dn)}: {last_line(exc)}", True
    rows, n_imprecise, n_err, n_skipped = [], 0, 0, 0
    for r in refs:
        kind = str(r.kind)
        if kind.endswith("no_ref"):
            continue
        node = r.ref
        if node.parent is not None and node.parent.is_a(lal.EndName):
            n_skipped += 1
            continue
        own = node
        if own.parent is not None and own.parent.is_a(lal.DottedName) and own.parent.f_suffix == own:
            own = own.parent
        if own.parent is not None and own.parent.is_a(lal.DefiningName):
            n_skipped += 1  # the name of a declaration part itself (spec/body/stub)
            continue
        tag = ""
        if kind.endswith("imprecise"):
            n_imprecise += 1
            tag = "  [approximate]"
        elif kind.endswith("error"):
            n_err += 1
            tag = "  [approximate: resolution error]"
        elif sess.has_errors(node.unit):
            tag = "  [approximate: file has parse errors]"
        ln = node.sloc_range.start.line
        rows.append((sess.rel(node.unit.filename), ln, f"{sess.rel(node.unit.filename)}:{ln}: "
                     f"{one_line(sess.line_text(node.unit, ln), 140)}{tag}"))
    rows.sort(key=lambda t: (t[0], t[1]))
    head = f"references to {_qualified(dn)} ({sess.loc(dn)}): {len(rows)}"
    notes = []
    if n_imprecise or n_err:
        notes.append(f"{n_imprecise} imprecise, {n_err} with resolution errors")
    if n_skipped:
        notes.append(f"{n_skipped} declaration/end-name occurrences omitted")
    if notes:
        head += " (" + "; ".join(notes) + ")"
    out = [head]
    if approx:
        out.insert(0, f"[approximate: {approx}]")
    out += [r[2] for r in rows[:limit]]
    if len(rows) > limit:
        out.append(f"[{len(rows) - limit} more not shown; raise limit to see them]")
    if not rows:
        out.append("(no references in the project sources)")
    return "\n".join(out), False
