"""find_unit and package_files."""

from __future__ import annotations

import os

from lal_mcp import deps
from lal_mcp.session import Session, ToolFailure, norm


def _lines(path: str) -> int:
    try:
        with open(path, "rb") as f:
            return f.read().count(b"\n")
    except OSError:
        return 0


def _part_line(sess: Session, p: deps.UnitPart, label: str | None = None) -> str:
    label = label or p.kind
    if not p.path:
        return f"{label:<8} {p.name}  unresolved: file for this subunit not found"
    extra = "" if sess.in_project(p.path) else "  [outside project sources]"
    return f"{label:<8} {sess.rel(p.path)}  ({_lines(p.path)} lines){extra}"


def _require_parts(sess: Session, name: str) -> list[deps.UnitPart]:
    parts = deps.find_parts(sess, name)
    if not parts:
        raise ToolFailure(
            f"unresolved: no unit named {name} in the project (checked spec and body via the GPR naming scheme)"
        )
    return parts


def find_unit(sess: Session, name: str) -> tuple[str, bool]:
    parts = _require_parts(sess, name)
    first = parts[0]
    cu = deps.cu_roots(sess.unit(first.path))
    decl_word = ""
    for c in cu:
        if deps.cu_name(c).lower() == name.lower():
            try:
                decl_word = deps.decl_kind_word(deps.cu_decl(c))
            except Exception:
                decl_word = ""
    out = [f"{first.name}" + (f"  ({decl_word})" if decl_word else "")]
    kinds = {p.kind for p in parts}
    for p in parts:
        out.append(_part_line(sess, p))
    if "spec" not in kinds:
        out.append("spec     none (body-only unit)")
    if "body" not in kinds and "subunit" not in kinds:
        out.append("body     none in project")
    approx = [p for p in parts if p.path and sess.has_errors(sess.unit(p.path))]
    for p in approx:
        out.append(f"[approximate] {sess.rel(p.path)} has parse errors: " + "; ".join(sess.diag_lines(sess.unit(p.path), 2)))
    return "\n".join(out), False


def package_files(sess: Session, name: str, children: bool, deps_: bool, specs_only: bool) -> tuple[str, bool]:
    parts = _require_parts(sess, name)
    out = [parts[0].name]
    own_paths = []
    for p in parts:
        if specs_only and p.kind != "spec":
            continue
        out.append(_part_line(sess, p))
        own_paths.append(p.path)
    body = next((p for p in parts if p.kind in ("body", "subunit")), None)
    if body is not None and not specs_only:
        for s in deps.subunits(sess, body.path):
            out.append(_part_line(sess, s, "separate") + f"  {s.name}")
            if s.path:
                own_paths.append(s.path)
    if children:
        kids = deps.children(sess, parts[0].name)
        if specs_only:
            kids = [k for k in kids if k.kind == "spec"]
        out.append(f"children: {len({k.name.lower() for k in kids})} unit(s)" + ("" if kids else " (none)"))
        for k in kids:
            out.append(f"  {k.name:<30} {k.kind:<5} {sess.rel(k.path)}")
            if not specs_only:
                for s in deps.subunits(sess, k.path) if k.kind == "body" else []:
                    out.append(f"  {s.name:<30} sep   {sess.rel(s.path) if s.path else 'unresolved: file not found'}")
    if deps_:
        all_paths = [p.path for p in parts if p.path]
        if body is not None:
            all_paths += [s.path for s in deps.subunits(sess, body.path) if s.path]
        cus = [cu for p in all_paths for cu in deps.cu_roots(sess.unit(p))]
        limited = deps.limited_withs(cus)
        closure = deps.closure(sess, all_paths)
        proj, ext = [], []
        for dep in closure:
            dname = deps.cu_name(dep)
            fn = dep.unit.filename
            if fn and os.path.isfile(fn) and sess.in_project(fn):
                proj.append((dname, fn))
            else:
                if dname.lower() != "standard":
                    ext.append(dname)
        proj.sort(key=lambda t: t[0].lower())
        out.append(f"deps: {len(proj)} project unit(s) in the closure (specs; bodies listed when present)")
        for dname, fn in proj:
            mark = "  [limited with]" if dname.lower() in limited else ""
            out.append(f"  {dname:<30} spec  {sess.rel(fn)}{mark}")
            if not specs_only:
                b = sess.unit_for_name(dname, body=True)
                if b is not None and norm(b.filename) != norm(fn):
                    out.append(f"  {'':<30} body  {sess.rel(b.filename)}")
        if ext:
            ext = sorted(set(ext), key=str.lower)
            out.append(f"runtime/external deps ({len(ext)}): " + ", ".join(ext))
    for p in parts:
        if p.path and sess.has_errors(sess.unit(p.path)):
            out.append(f"[approximate] {sess.rel(p.path)} has parse errors; dependency info may be incomplete")
    return "\n".join(out), False
