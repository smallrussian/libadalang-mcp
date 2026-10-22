"""Unit naming, unit index, subunits, children and dependency closure."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from lal_mcp.session import Session, norm


@dataclass(frozen=True)
class UnitPart:
    name: str  # proper-case dotted name as written in the source
    kind: str  # "spec" | "body" | "subunit"
    path: str  # absolute
    line: int


def cu_roots(unit) -> list:
    """Compilation units in an analysis unit (a file may hold several)."""
    root = unit.root
    if root is None:
        return []
    lal = _lal(root)
    if root.is_a(lal.CompilationUnit):
        return [root]
    return [c for c in root.children if c is not None and c.is_a(lal.CompilationUnit)]


def _lal(node):
    import libadalang

    return libadalang


def cu_name(cu) -> str:
    """Dotted unit name with the source's own casing."""
    lal = _lal(cu)
    body = cu.f_body
    try:
        if body.is_a(lal.Subunit):
            return f"{body.f_name.text}.{body.f_body.p_defining_name.text}"
        return body.f_item.p_defining_name.text
    except Exception:
        return ".".join(cu.p_syntactic_fully_qualified_name)


def cu_kind(cu) -> str:
    lal = _lal(cu)
    if cu.f_body.is_a(lal.Subunit):
        return "subunit"
    return "body" if cu.p_unit_kind == lal.AnalysisUnitKind.unit_body else "spec"


def cu_decl(cu):
    """The library item / subunit body declaration of a compilation unit."""
    lal = _lal(cu)
    body = cu.f_body
    return body.f_body if body.is_a(lal.Subunit) else body.f_item


def decl_kind_word(decl) -> str:
    """Short human word for a unit's declaration kind (package, procedure, generic package...)."""
    k = decl.kind_name
    if "Package" in k:
        word = "package"
    elif "Subp" in k or "ExprFunction" in k:
        spec = decl.p_subp_spec_or_null()
        word = spec.f_subp_kind.text.lower() if spec is not None else "subprogram"
    elif "Task" in k:
        word = "task"
    elif "Protected" in k:
        word = "protected"
    else:
        word = k
    if k.startswith("Generic") and "Instantiation" not in k:
        word = "generic " + word
    elif "Instantiation" in k:
        word += " instantiation"
    return word


# --------------------------------------------------------------- unit index


_INDEX_ATTR = "_unit_index"


def unit_index(sess: Session) -> list[UnitPart]:
    """Every compilation unit in the project sources, parsed once and refreshed on mtime change."""
    cache: dict[str, tuple[float, list[UnitPart]]] = getattr(sess, _INDEX_ATTR, None) or {}
    out: list[UnitPart] = []
    for path in sess.sources():
        key = norm(path)
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        hit = cache.get(key)
        if hit is None or hit[0] != mtime:
            parts = []
            for cu in cu_roots(sess.unit(path)):
                parts.append(UnitPart(cu_name(cu), cu_kind(cu), path, cu.sloc_range.start.line))
            hit = (mtime, parts)
            cache[key] = hit
        out.extend(hit[1])
    setattr(sess, _INDEX_ATTR, cache)
    return out


def find_parts(sess: Session, name: str) -> list[UnitPart]:
    """Spec/body/subunit parts for a unit name, via the project's unit provider."""
    parts: list[UnitPart] = []
    for body in (False, True):
        u = sess.unit_for_name(name, body=body)
        if u is None:
            continue
        for cu in cu_roots(u):
            if cu_name(cu).lower() == name.lower():
                parts.append(UnitPart(cu_name(cu), cu_kind(cu), u.filename, cu.sloc_range.start.line))
    return parts


def subunits(sess: Session, body_path: str, seen: set[str] | None = None) -> list[UnitPart]:
    """`is separate` subunits of a body, recursively (subunits can have subunits)."""
    seen = seen if seen is not None else set()
    out: list[UnitPart] = []
    unit = sess.unit(body_path)
    for cu in cu_roots(unit):
        lal = _lal(cu)
        parent = cu_name(cu)
        for stub in cu.findall(lal.BodyStub):
            try:
                sub_name = f"{parent}.{stub.p_defining_name.text}"
            except Exception:
                continue
            u = sess.unit_for_name(sub_name, body=True)
            if u is None:
                out.append(UnitPart(sub_name, "subunit", "", stub.sloc_range.start.line))
                continue
            key = norm(u.filename)
            if key in seen:
                continue
            seen.add(key)
            out.append(UnitPart(sub_name, "subunit", u.filename, 1))
            out.extend(subunits(sess, u.filename, seen))
    return out


def children(sess: Session, name: str) -> list[UnitPart]:
    """Child units (all descendants) of `name` among the project sources, excluding subunits."""
    prefix = name.lower() + "."
    return sorted(
        (p for p in unit_index(sess) if p.kind != "subunit" and p.name.lower().startswith(prefix)),
        key=lambda p: (p.name.lower(), p.kind != "spec"),
    )


def closure(sess: Session, paths: list[str]) -> list[Any]:
    """Transitive dependency closure (CompilationUnits) of the units in `paths`, deduplicated."""
    seen: set[tuple[str, str]] = set()
    own = {norm(p) for p in paths}
    out = []
    for path in paths:
        for cu in cu_roots(sess.unit(path)):
            try:
                deps = cu.p_unit_dependencies
            except Exception:
                continue
            for dep in deps:
                fn = dep.unit.filename
                key = (norm(fn), ".".join(dep.p_syntactic_fully_qualified_name))
                if key in seen or norm(fn) in own:
                    continue
                seen.add(key)
                out.append(dep)
    return out


def limited_withs(cus: list) -> set[str]:
    """Lower-case names of units mentioned in `limited with` clauses."""
    names: set[str] = set()
    for cu in cus:
        lal = _lal(cu)
        for wc in cu.f_prelude.findall(lal.WithClause):
            if wc.f_has_limited.text:  # LimitedPresent has text "limited", LimitedAbsent ""
                for n in wc.f_packages:
                    names.add(n.text.lower())
    return names
