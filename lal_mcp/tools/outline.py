"""outline: declarations with line numbers, no bodies."""

from __future__ import annotations

from lal_mcp import deps
from lal_mcp.session import Session, ToolFailure, looks_like_unit_name
from lal_mcp.tools.text import one_line


def _target_paths(sess: Session, target: str) -> tuple[str, str | None]:
    """(path to outline, note). Unit names outline the spec (or the body when there is none)."""
    t = target.strip()
    if looks_like_unit_name(t):
        parts = deps.find_parts(sess, t)
        if not parts:
            raise ToolFailure(f"unresolved: no unit named {t} in the project")
        spec = next((p for p in parts if p.kind == "spec"), None)
        others = [p for p in parts if p is not spec]
        if spec is not None:
            note = f"body: {sess.rel(others[0].path)} (outline that path for the body)" if others else None
            return spec.path, note
        return parts[0].path, "no spec: body-only unit"
    return sess.resolve_path(t), None


class _Outliner:
    def __init__(self, sess: Session):
        import libadalang as lal

        self.lal = lal
        self.sess = sess
        self.rows: list[tuple[int, int, str]] = []  # (line, depth, text)

    def add(self, node, depth: int, text: str) -> None:
        self.rows.append((node.sloc_range.start.line, depth, text))

    def decls(self, lst, depth: int) -> None:
        if lst is None:
            return
        for d in lst:
            if d is not None:
                self.decl(d, depth)

    def part(self, part, depth: int) -> None:
        """A PublicPart/PrivatePart/DeclarativePart."""
        if part is not None:
            self.decls(part.f_decls, depth)

    def decl(self, d, depth: int) -> None:
        lal = self.lal
        if not d.is_a(lal.BasicDecl):
            return  # pragmas, use clauses, representation clauses
        if d.is_a(lal.GenericPackageDecl):
            self.generic_formals(d, depth)
            self.package_spec(d.f_package_decl, depth, "generic package")
        elif d.is_a(lal.PackageDecl):
            self.package_spec(d, depth, "package")
        elif d.is_a(lal.PackageBody):
            self.add(d, depth, f"package body {d.p_defining_name.text}")
            self.part(d.f_decls, depth + 1)
        elif d.is_a(lal.PackageBodyStub, lal.TaskBodyStub, lal.ProtectedBodyStub):
            self.add(d, depth, one_line(d.text))
        elif d.is_a(lal.TaskTypeDecl, lal.SingleTaskDecl, lal.ProtectedTypeDecl, lal.SingleProtectedDecl):
            self.concurrent_spec(d, depth)
        elif d.is_a(lal.ProtectedBody, lal.TaskBody):
            kw = "protected body" if d.is_a(lal.ProtectedBody) else "task body"
            self.add(d, depth, f"{kw} {d.p_defining_name.text}")
            if d.is_a(lal.ProtectedBody):
                self.part(d.f_decls, depth + 1)
        elif d.is_a(lal.GenericSubpDecl):
            self.generic_formals(d, depth)
            self.add(d.f_subp_decl, depth, one_line(d.f_subp_decl.f_subp_spec.text) + ";")
        elif d.is_a(lal.EntryDecl):
            self.add(d, depth, one_line(d.text))
        elif d.is_a(lal.EntryBody):
            self.add(d, depth, f"entry body {d.p_defining_name.text}")
        elif d.is_a(lal.ExprFunction, lal.SubpRenamingDecl, lal.NullSubpDecl, lal.AbstractSubpDecl):
            self.add(d, depth, one_line(d.text))
        elif d.is_a(lal.SubpBodyStub):
            self.add(d, depth, one_line(d.f_subp_spec.text) + " is separate;")
        elif d.is_a(lal.BaseSubpBody, lal.BasicSubpDecl):
            spec = d.p_subp_spec_or_null()
            text = one_line(spec.text) if spec is not None else one_line(d.text)
            self.add(d, depth, text + (" is ...;" if d.is_a(lal.BaseSubpBody) else ";"))
        elif d.is_a(lal.TypeDecl) and d.f_type_def is not None and d.f_type_def.is_a(lal.RecordTypeDef, lal.DerivedTypeDef):
            self.record_type(d, depth)
        else:
            self.add(d, depth, one_line(d.text))

    def package_spec(self, d, depth: int, kw: str) -> None:
        self.add(d, depth, f"{kw} {d.p_defining_name.text}")
        self.part(d.f_public_part, depth + 1)
        if d.f_private_part is not None:
            self.add(d.f_private_part, depth, "private")
            self.part(d.f_private_part, depth + 1)

    def concurrent_spec(self, d, depth: int) -> None:
        lal = self.lal
        kw = "task" if d.is_a(lal.TaskTypeDecl, lal.SingleTaskDecl) else "protected"
        if d.is_a(lal.TaskTypeDecl, lal.ProtectedTypeDecl):
            kw += " type"
        self.add(d, depth, f"{kw} {d.p_defining_name.text}")
        definition = getattr(d, "f_definition", None)
        if definition is None and hasattr(d, "f_task_type"):
            definition = d.f_task_type.f_definition
        if definition is None:
            return
        self.part(definition.f_public_part, depth + 1)
        if definition.f_private_part is not None:
            self.add(definition.f_private_part, depth, "private")
            self.part(definition.f_private_part, depth + 1)

    def generic_formals(self, d, depth: int) -> None:
        formals = d.f_formal_part.f_decls
        names = [one_line(f.text, 80) for f in formals if f is not None and f.is_a(self.lal.BasicDecl)]
        self.add(d, depth, "generic " + ("  ".join(names) if names else ""))

    def record_type(self, d, depth: int) -> None:
        lal = self.lal
        head = one_line(d.text.split("record")[0], 120) + " record"
        if d.f_type_def.is_a(lal.DerivedTypeDef) and d.f_type_def.f_record_extension is None:
            self.add(d, depth, one_line(d.text))
            return
        self.add(d, depth, head)
        for comp in d.f_type_def.findall(lal.ComponentDecl):
            self.add(comp, depth + 1, one_line(comp.text, 120))
        for variant in d.f_type_def.findall(lal.VariantPart):
            self.add(variant, depth + 1, f"case {variant.f_discr_name.text} is ... (variant part)")


def outline(sess: Session, target: str) -> tuple[str, bool]:
    path, note = _target_paths(sess, target)
    unit = sess.unit(path)
    cus = deps.cu_roots(unit)
    if not cus:
        return f"unresolved: {sess.rel(path)} could not be parsed at all", True
    out: list[str] = []
    n_lines = unit.text.count("\n") + 1
    for cu in cus:
        o = _Outliner(sess)
        o.decl(deps.cu_decl(cu), 0)
        withs = []
        for wc in cu.f_prelude:
            if wc.is_a(o.lal.WithClause):
                lim = "limited " if wc.f_has_limited.text else ""
                withs += [f"{lim}{n.text}" for n in wc.f_packages]
        out.append(f"{sess.rel(path)}  {deps.cu_kind(cu)} of {deps.cu_name(cu)}  ({n_lines} lines)")
        if withs:
            out.append("with: " + ", ".join(withs))
        width = len(str(max((r[0] for r in o.rows), default=1)))
        for line, depth, text in sorted(o.rows, key=lambda r: r[0]):  # stable: keeps nesting order per line
            out.append(f"{line:>{width}}  {'  ' * depth}{text}")
    if note:
        out.append(note)
    if sess.has_errors(unit):
        out.insert(0, "[approximate] file has parse errors; outline may be incomplete:\n  " +
                   "\n  ".join(sess.diag_lines(unit, 3)))
    return "\n".join(out), False
