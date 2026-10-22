"""Structured tools against the fixture project (skipped without libadalang)."""

from __future__ import annotations

import pytest

from conftest import FIXTURE_GPR, requires_lal
from lal_mcp.session import Config, Session, ToolFailure
from lal_mcp.tools import navigate, outline, units

pytestmark = requires_lal


@pytest.fixture(scope="module")
def s() -> Session:
    sess = Session(Config(project=FIXTURE_GPR))
    sess.ensure_loaded()
    return sess


# ---------------------------------------------------------------- find_unit


def test_find_unit_non_default_naming(s):
    text, err = units.find_unit(s, "vehicle.engine")
    assert not err
    assert "Vehicle.Engine  (package)" in text
    assert "spec     src/vehicle__engine.1.ada" in text
    assert "body     src/vehicle__engine.2.ada" in text


def test_find_unit_explicit_file_names(s):
    text, _ = units.find_unit(s, "Utils")
    assert "src/utils_spec.ada" in text and "src/utils_body.ada" in text


def test_find_unit_body_only(s):
    text, _ = units.find_unit(s, "Main_Proc")
    assert "body     src/main_proc.2.ada" in text
    assert "spec     none (body-only unit)" in text


def test_find_unit_unknown_is_unresolved(s):
    with pytest.raises(ToolFailure, match="unresolved: no unit named Nope"):
        units.find_unit(s, "Nope")


def test_find_unit_syntax_error_is_approximate(s):
    text, _ = units.find_unit(s, "Broken")
    assert "[approximate]" in text and "parse errors" in text


# ------------------------------------------------------------ package_files


def test_package_files_separate_and_children(s):
    text, _ = units.package_files(s, "Vehicle", True, False, False)
    assert "Vehicle.Engine                 spec  src/vehicle__engine.1.ada" in text
    assert "Vehicle.Engine.Start           sep   src/vehicle__engine__start.2.ada" in text


def test_package_files_subunit_of_own_body(s):
    text, _ = units.package_files(s, "Vehicle.Engine", False, False, False)
    assert "separate src/vehicle__engine__start.2.ada" in text
    assert "children" not in text


def test_package_files_deps_and_limited_with(s):
    text, _ = units.package_files(s, "Garage", False, True, True)
    assert "Vehicle.Engine                 spec  src/vehicle__engine.1.ada  [limited with]" in text
    assert "Vehicle                        spec  src/vehicle.1.ada" in text
    assert "body" not in text.split("deps:")[1]  # specs_only


def test_package_files_deps_lists_runtime_names(s):
    text, _ = units.package_files(s, "Vehicle", False, True, False)
    assert "runtime/external deps" in text and "Ada.Text_IO" in text


# ------------------------------------------------------------------ outline


def test_outline_spec_has_signatures_not_bodies(s):
    text, _ = outline.outline(s, "Vehicle")
    assert "src/vehicle.1.ada  spec of Vehicle" in text
    assert "13    procedure Log (Msg : String);" in text
    assert "14    procedure Log (Value : Integer);" in text
    assert " 9      X, Y : Integer := 0;" in text  # record components
    assert "body: src/vehicle.2.ada" in text


def test_outline_body_has_no_statements(s):
    text, _ = outline.outline(s, "src/vehicle__engine.2.ada")
    assert "procedure Start (E : in out Engine) is separate;" in text
    assert "procedure Accelerate (E : in out Engine; By : Speed) is ...;" in text
    assert "Clamp" not in text  # statement inside a body


def test_outline_limited_with(s):
    text, _ = outline.outline(s, "Garage")
    assert "with: limited Vehicle.Engine" in text


def test_outline_syntax_error_is_approximate(s):
    text, err = outline.outline(s, "broken.2.ada")
    assert not err
    assert text.startswith("[approximate]")


# ---------------------------------------------------------- goto_definition


def test_goto_picks_right_overload(s):
    t1, _ = navigate.goto_definition(s, "src/main_proc.2.ada", 13, 4)
    t2, _ = navigate.goto_definition(s, "src/main_proc.2.ada", 14, 4)
    assert "src/vehicle.1.ada:13\nprocedure Log (Msg : String);" in t1
    assert "src/vehicle.1.ada:14\nprocedure Log (Value : Integer);" in t2


def test_goto_lists_stub_and_separate_body(s):
    text, _ = navigate.goto_definition(s, "src/main_proc.2.ada", 10, 19)
    assert "Vehicle.Engine.Start  [SubpDecl]" in text
    assert "stub (is separate) src/vehicle__engine.2.ada:5" in text
    assert "body               src/vehicle__engine__start.2.ada:2" in text


def test_goto_shows_record_text(s):
    text, _ = navigate.goto_definition(s, "src/main_proc.2.ada", 7, 34)
    assert "type Engine is tagged record" in text and "Running : Boolean := False;" in text


def test_goto_unresolvable_name(s):
    text, err = navigate.goto_definition(s, "src/bad_ref.2.ada", 7, 12)
    assert text == "unresolved: Vehicle.Does_Not_Exist at src/bad_ref.2.ada:7:12"
    assert not err


def test_goto_in_file_with_syntax_error_is_approximate(s):
    text, _ = navigate.goto_definition(s, "src/broken.2.ada", 7, 12)
    assert text.startswith("[approximate: file has parse errors]")


def test_goto_not_on_a_name_lists_names(s):
    with pytest.raises(ToolFailure, match=r"no name at .*Start \(col 19\)"):
        navigate.goto_definition(s, "src/main_proc.2.ada", 10, 2)


def test_goto_bad_line(s):
    with pytest.raises(ToolFailure, match="out of range"):
        navigate.goto_definition(s, "src/main_proc.2.ada", 999, 1)


# ---------------------------------------------------------- find_references


def test_references_respect_overloads(s):
    t_int, _ = navigate.find_references(s, "src/vehicle.1.ada", 14, 14, 50)
    t_str, _ = navigate.find_references(s, "src/vehicle.1.ada", 13, 14, 50)
    assert "src/main_proc.2.ada:14: Log (Integer (Car.Current));" in t_int
    assert "src/main_proc.2.ada:13" not in t_int
    assert "src/main_proc.2.ada:13: Log (\"speed:\");" in t_str
    assert "src/vehicle__engine__start.2.ada:5:" in t_str  # call from the separate body


def test_references_in_broken_file_marked(s):
    text, _ = navigate.find_references(s, "src/vehicle.1.ada", 14, 14, 50)
    assert "src/broken.2.ada:7: Vehicle.Log (X);  [approximate: file has parse errors]" in text


def test_references_limit(s):
    text, _ = navigate.find_references(s, "src/vehicle__engine.1.ada", 2, 17, 2)
    assert "more not shown; raise limit" in text


def test_references_unresolved(s):
    text, err = navigate.find_references(s, "src/bad_ref.2.ada", 7, 12, 50)
    assert text.startswith("unresolved:") and not err


def test_references_omit_dotted_end_names(s):
    text, _ = navigate.find_references(s, "src/vehicle__engine.1.ada", 2, 17, 100)
    assert "end Vehicle.Engine;" not in text
    assert "package body Vehicle.Engine" not in text
    assert "src/main_proc.2.ada:2: with Vehicle.Engine;" in text


def test_test_model_includes_documentation_as_specification(s):
    from lal_mcp.tools.testmodel import test_model

    text, _ = test_model(s, "src/vehicle.1.ada", 14, 0)  # procedure Log (Value : Integer), commented below it
    assert "documentation (src/vehicle.1.ada:14), the specification for expected values:" in text
    assert "  | Two overloads: name resolution must pick the right one." in text
    text, _ = test_model(s, "src/vehicle.1.ada", 17, 0)  # Distance: no comment
    assert "documentation: none on the declaration" in text
