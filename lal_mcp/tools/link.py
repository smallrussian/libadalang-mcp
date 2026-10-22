"""link_info: build/link metadata for a unit and its dependency closure.

For every foreign import/export (pragma Import/Export/Interface or the Import/
Export aspects): convention, Ada entity, external symbol, and where the symbol
is defined (C/C++ source of the project tree, a project library, or the GNAT
runtime). Plus the GPR projects that own the closure (the `with` clauses a
client needs), their linker options, runtime units, and missing bodies.

The first line is a status for workflow gates: `[status: complete]` or
`[status: INCOMPLETE: ...]` listing everything that could not be established.
"""

from __future__ import annotations

import os

from lal_mcp import deps
from lal_mcp.buildinfo import BuildInfo
from lal_mcp.session import Session, ToolFailure, norm

DEFAULT_LINK_CASE = {"c", "cpp", "c_plus_plus", "fortran", "stdcall", "cobol", "asm", "assembler"}


def _buildinfo(sess: Session) -> BuildInfo:
    bi = getattr(sess, "_buildinfo", None)
    if bi is None:
        bi = BuildInfo(sess)
        sess._buildinfo = bi
    return bi


def _pragma_args(p) -> tuple[list[str], dict[str, str]]:
    pos, named = [], {}
    for a in p.f_args:
        expr = a.f_expr.text if a.f_expr is not None else a.text
        if a.f_name is not None:
            named[a.f_name.text.lower()] = expr
        else:
            pos.append(expr)
    return pos, named


def _unquote(s: str | None) -> str | None:
    if s is None:
        return None
    s = s.strip()
    return s[1:-1] if len(s) >= 2 and s[0] == s[-1] == '"' else s


def foreign_items(lal, unit) -> list[dict]:
    """Import/Export/Interface declarations in one analysis unit."""
    items = []
    if unit.root is None:
        return items
    for p in unit.root.findall(lal.PragmaNode):
        kind = p.f_id.text.lower()
        if kind not in ("import", "export", "interface"):
            continue
        pos, named = _pragma_args(p)
        conv = named.get("convention") or (pos[0] if pos else None)
        entity = named.get("entity") or (pos[1] if len(pos) > 1 else None)
        ext = named.get("external_name") or (pos[2] if len(pos) > 2 else None)
        link = named.get("link_name") or (pos[3] if len(pos) > 3 else None)
        items.append({"node": p, "form": "pragma", "kind": "export" if kind == "export" else "import",
                      "convention": conv, "entity": entity, "external": _unquote(link or ext)})
    for d in unit.root.findall(lal.BasicDecl):
        # Only real `with Import, ...` aspect clauses: p_has_aspect also answers True for a
        # pragma Import on the same entity, which is already listed above.
        aspects = getattr(d, "f_aspects", None)
        if aspects is None:
            continue
        names = {a.f_id.text.lower() for a in aspects.f_aspect_assocs}
        imp, exp = "import" in names, "export" in names
        if not (imp or exp):
            continue
        conv = d.p_get_aspect_spec_expr("Convention")
        link = d.p_get_aspect_spec_expr("Link_Name")
        ext = d.p_get_aspect_spec_expr("External_Name")
        try:
            name = d.p_defining_name.text
        except Exception:
            name = "?"
        items.append({"node": d, "form": "aspect", "kind": "export" if exp else "import",
                      "convention": conv.text if conv is not None else None, "entity": name,
                      "external": _unquote((link or ext).text) if (link or ext) is not None else None})
    return items


def _symbol(item: dict) -> str | None:
    """Link symbol: the explicit external/link name, else GNAT's default (lower-cased Ada name)."""
    conv = (item["convention"] or "").lower()
    if conv == "intrinsic":
        return None
    if item["external"]:
        return item["external"]
    ent = (item["entity"] or "").split(".")[-1]
    return ent.lower() if conv in DEFAULT_LINK_CASE else ent


def _requires_body_missing(lal, spec_unit) -> list:
    """Subprogram declarations in a spec with no completion anywhere in the project."""
    missing = []
    for cu in deps.cu_roots(spec_unit):
        decl = deps.cu_decl(cu)
        nodes = [decl] if decl.is_a(lal.BasicSubpDecl) else list(decl.findall(lal.BasicSubpDecl))
        for d in nodes:
            if d.is_a(lal.AbstractSubpDecl, lal.NullSubpDecl, lal.ExprFunction, lal.SubpRenamingDecl,
                      lal.EnumLiteralDecl) or d.is_a(lal.GenericSubpInternal):
                continue
            if any(p.is_a(lal.GenericFormalPart) for p in d.parents(with_self=False)):
                continue
            try:
                if d.p_is_imported:
                    continue
                if d.p_body_part_for_decl() is None:
                    missing.append(d)
            except lal.PropertyError:
                continue
    return missing


def link_info(sess: Session, name: str, with_deps: bool) -> tuple[str, bool]:
    import libadalang as lal

    parts = deps.find_parts(sess, name)
    if not parts:
        raise ToolFailure(f"unresolved: no unit named {name} in the project")
    bi = _buildinfo(sess)
    gaps: list[str] = []

    # --- units: the unit's own parts (+ subunits), then its project dependency closure
    paths: dict[str, str] = {}  # norm path -> path
    for p in parts:
        paths[norm(p.path)] = p.path
        if p.kind in ("body", "subunit"):
            for s in deps.subunits(sess, p.path):
                if s.path:
                    paths[norm(s.path)] = s.path
    runtime_units: set[str] = set()
    project_dep_names: list[str] = []
    if with_deps:
        for dep in deps.closure(sess, [p.path for p in parts]):
            fn = dep.unit.filename
            dname = deps.cu_name(dep)
            if fn and os.path.isfile(fn) and sess.in_project(fn):
                project_dep_names.append(dname)
                paths[norm(fn)] = fn
                b = sess.unit_for_name(dname, body=True)
                if b is not None:
                    paths[norm(b.filename)] = b.filename
                    for s in deps.subunits(sess, b.filename):
                        if s.path:
                            paths[norm(s.path)] = s.path
            elif dname.lower() != "standard":
                runtime_units.add(dname)

    # --- foreign imports/exports and where their symbols live
    runtime_dir = None
    sysu = sess.unit_for_name("System", body=False)
    if sysu is not None:
        runtime_dir = os.path.join(os.path.dirname(os.path.dirname(sysu.filename)), "adalib")
    libs = bi.candidate_libraries(runtime_dir)
    lines_imp: list[str] = []
    n_items = 0
    for path in sorted(paths.values(), key=lambda p: sess.rel(p)):
        unit = sess.unit(path)
        if sess.has_errors(unit):
            gaps.append(f"parse errors in {sess.rel(path)}")
        for it in foreign_items(lal, unit):
            n_items += 1
            node = it["node"]
            sym = _symbol(it)
            head = (f"  {sess.loc(node)}  {it['entity']}  {it['kind']}, convention {it['convention'] or '?'}"
                    f" ({it['form']})")
            if sym is None:
                lines_imp.append(head + ", compiler intrinsic: no link symbol")
                continue
            default = "" if it["external"] else " (default external name)"
            lines_imp.append(head + f', symbol "{sym}"{default}')
            if it["kind"] == "export":
                lines_imp.append("      exported from Ada: defined by this declaration")
                continue
            found = False
            cdefs = bi.c_definitions(sym)
            if len(cdefs) > 1:
                lines_imp.append(f"      {len(cdefs)} C/C++ definitions: probably #if/#ifdef platform variants; "
                                 "check which one the build compiles")
            for f, line, proj, what in cdefs:
                found = True
                lines_imp.append(f"      defined: {sess.rel(f)}:{line} {what}, project {proj.name} "
                                 f"({sess.rel(proj.file)}) [approximate: C/C++ text search]")
            for lib, owner in libs:
                syms = bi.nm_symbols(lib)
                if syms and (sym in syms or "_" + sym in syms):
                    found = True
                    lines_imp.append(f"      defined: {sess.rel(lib)} ({owner}) [nm]")
            if not found:
                checked = f"{len(bi.c_files())} C/C++ files, {len(libs)} libraries"
                lines_imp.append(f'      unresolved: no definition of "{sym}" found ({checked}); '
                                 "it may come from a linker option or a library outside the project tree")
                gaps.append(f'unresolved symbol "{sym}"')

    # --- projects owning the closure: the with clauses a client project needs
    owners: dict[str, tuple] = {}
    for path in paths.values():
        proj = bi.project_of(path)
        if proj is None:
            gaps.append(f"no GPR project owns {sess.rel(path)}")
            continue
        o = owners.setdefault(norm(proj.file), (proj, set()))
        for cu in deps.cu_roots(sess.unit(path)):
            o[1].add(deps.cu_name(cu))
    lines_proj = []
    for proj, units in sorted(owners.values(), key=lambda t: t[0].name.lower()):
        lib = ""
        if proj.library_file:
            built = "built" if os.path.isfile(proj.library_file) else "not built yet"
            lib = f", library {os.path.basename(proj.library_file)} ({built})"
        ext = ", externally built" if proj.externally_built else ""
        lines_proj.append(f"  {proj.name}  {sess.rel(proj.file)}  {proj.kind}{lib}{ext}; languages "
                          f"{', '.join(proj.languages) or '?'}; withed by root: {bi.withed_by_root(proj)}")
        lines_proj.append(f"      units: {', '.join(sorted(units, key=str.lower))}")
        if proj.linker_options:
            lines_proj.append(f"      linker options: {' '.join(proj.linker_options)}")
        if bi.withed_by_root(proj) == "no":
            gaps.append(f"project {proj.name} is not withed by the root project")

    # --- missing bodies
    lines_missing = []
    for path in paths.values():
        if not path.lower().endswith((".ads",)) and not any(
                cu.p_unit_kind == lal.AnalysisUnitKind.unit_specification for cu in deps.cu_roots(sess.unit(path))):
            continue
        for d in _requires_body_missing(lal, sess.unit(path)):
            lines_missing.append(f"  {sess.loc(d)}  {d.p_defining_name.text}: no body in the project")
            gaps.append(f"missing body for {d.p_defining_name.text}")

    if sess.project_errors:
        gaps.append("project warnings: " + "; ".join(sess.project_errors[:2]))
    gaps += [f"gprinspect: {w}" for w in bi.warnings[:2]]

    unique_gaps = list(dict.fromkeys(gaps))
    status = "[status: complete]" if not unique_gaps else "[status: INCOMPLETE: " + "; ".join(unique_gaps) + "]"
    out = [status,
           f"link info for {parts[0].name}: {len(paths)} project files in "
           f"{len(owners)} project(s), {len(runtime_units)} runtime units"
           + ("" if with_deps else " (deps=false: the unit's own files only)")]
    out.append(f"foreign imports/exports ({n_items}):" + ("" if n_items else " none"))
    out += lines_imp
    out.append("projects (a client project needs a `with` for each non-root one):")
    out += lines_proj
    if lines_missing:
        out.append("missing bodies:")
        out += lines_missing
    if runtime_units:
        out.append(f"runtime units (GNAT runtime, linked automatically): {', '.join(sorted(runtime_units, key=str.lower))}")
    return "\n".join(out), False
