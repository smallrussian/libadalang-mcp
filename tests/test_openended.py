"""run_lal, api_help, dump against the fixture (skipped without libadalang)."""

from __future__ import annotations

import pytest

from conftest import FIXTURE_GPR, requires_lal
from lal_mcp.server import INSTRUCTIONS
from lal_mcp.session import Config, Session, ToolFailure
from lal_mcp.tools import openended as o

pytestmark = requires_lal


@pytest.fixture(scope="module")
def s() -> Session:
    sess = Session(Config(project=FIXTURE_GPR, timeout=2))
    sess.ensure_loaded()
    return sess


# ------------------------------------------------------------------ run_lal


def test_run_lal_prints_and_uses_helpers(s):
    text, err = o.run_lal(s, 'for d in unit("Vehicle").root.findall(lal.BasicSubpDecl):\n'
                             '    print(rel(d), d.p_defining_name.text)')
    assert not err
    assert text.splitlines() == ["src/vehicle.1.ada:13 Log", "src/vehicle.1.ada:14 Log",
                                 "src/vehicle.1.ada:17 Distance"]


def test_run_lal_final_expression_is_shown(s):
    text, err = o.run_lal(s, 'n = unit("src/main_proc.2.ada").root.lookup(lal.Sloc(14, 4))\n'
                             'rel(n.p_referenced_decl())')
    assert not err and text.strip() == "'src/vehicle.1.ada:14'"


def test_run_lal_exception_is_error_with_snippet_line(s):
    text, err = o.run_lal(s, 'print("before")\nx = unit("Vehicle").root.f_nope')
    assert err
    assert text.startswith("before\n")
    assert "AttributeError" in text and "(snippet line 2)" in text


def test_run_lal_unknown_unit_says_unresolved(s):
    text, err = o.run_lal(s, 'unit("Nope.Missing")')
    assert err and "unresolved: no unit named Nope.Missing" in text


def test_run_lal_syntax_error(s):
    text, err = o.run_lal(s, "for x in")
    assert err and "SyntaxError" in text


def test_run_lal_timeout(s):
    text, err = o.run_lal(s, "while True:\n    pass")
    assert err and "timed out after 2s" in text


def test_run_lal_does_not_touch_real_stdout(s, capfd):
    o.run_lal(s, 'print("captured")')
    assert "captured" not in capfd.readouterr().out


def test_instruction_examples_run(s):
    """Every run_lal example in the server instructions must work (names adapted to the fixture)."""
    lines = INSTRUCTIONS.splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.strip().startswith("# "))
    blocks, cur = [], []
    for ln in lines[start:]:
        if ln.strip().startswith("# "):
            if cur:
                blocks.append(cur)
            cur = []
        elif ln.strip():
            cur.append(ln[2:])
    blocks.append(cur)
    assert len(blocks) >= 4
    subst = [('"Pkg"', '"Vehicle"'), ('"Op"', '"Log"'), ('"Rec"', '"Position"'),
             ('"src/x.adb"', '"src/main_proc.2.ada"'), ("Sloc(42, 7)", "Sloc(14, 4)")]
    for block in blocks:
        code = "\n".join(block)
        for a, b in subst:
            code = code.replace(a, b)
        text, err = o.run_lal(s, code)
        assert not err, (code, text)
        assert "src/" in text, (code, text)


# ----------------------------------------------------------------- api_help


def test_api_help_class(s):
    text, err = o.api_help(s, "WithClause")
    assert not err
    assert text.startswith("class WithClause(AdaNode)")
    assert "f_has_limited, f_has_private, f_packages" in text
    assert ":py:class:" not in text and "RM 10.1.2" in text


def test_api_help_method_and_property(s):
    text, _ = o.api_help(s, "Name.p_referenced_decl")
    assert "imprecise_fallback: 'bool' = False" in text
    text, _ = o.api_help(s, "lal.BasicDecl.p_fully_qualified_name")
    assert "(property) -> str" in text and "PreconditionFailure" in text


def test_api_help_typo_suggests(s):
    text, err = o.api_help(s, "Name.p_referenced_dec")
    assert err and text.startswith("unresolved:") and "p_referenced_decl" in text


# --------------------------------------------------------------------- dump


def test_dump_whole_declaration_for_its_lines(s):
    text, err = o.dump(s, "src/vehicle.1.ada", 8, 11, 3)
    assert not err
    body = text.splitlines()
    assert body[1].startswith("ConcreteTypeDecl 8:4-11:15")
    assert '    f_name: Identifier 8:9-8:17 "Position"' in body
    assert "    f_has_tagged: TaggedAbsent" in body  # placeholder nodes without slocs


def test_dump_list_items_in_range(s):
    text, _ = o.dump(s, "Vehicle", 13, 14, 2)
    assert text.count("\nSubpDecl ") == 2
    assert '    f_subp_kind: SubpKindProcedure 13:4-13:13 "procedure"' in text


def test_dump_depth_limit_summarizes(s):
    text, _ = o.dump(s, "src/main_proc.2.ada", 14, 14, 2)
    assert '... [1 children] "Integer (Car.Current)"' in text or "children]" in text


def test_dump_parse_errors_marked(s):
    text, _ = o.dump(s, "broken.2.ada", 5, 5, 2)
    assert text.startswith("[approximate]")


def test_dump_bad_range(s):
    with pytest.raises(ToolFailure):
        o.dump(s, "Vehicle", 10, 5, 2)
