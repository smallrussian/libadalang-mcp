"""link_info and test_model against tests/fixture_link (an app project withing a HAL library
project whose Ada bindings import C symbols). Needs libadalang, plus gprinspect/nm on PATH."""

from __future__ import annotations

import os
import shutil

import pytest

from conftest import TESTS, requires_lal
from lal_mcp.session import Config, Session, ToolFailure

LINK_FIXTURE = os.path.join(TESTS, "fixture_link")

pytestmark = [requires_lal,
              pytest.mark.skipif(not shutil.which("gprinspect"), reason="gprinspect (GNAT bin) not on PATH")]


@pytest.fixture(scope="module")
def s() -> Session:
    sess = Session(Config(project=os.path.join(LINK_FIXTURE, "app", "app.gpr"), workspace=LINK_FIXTURE))
    sess.ensure_loaded()
    return sess


# ---------------------------------------------------------------- link_info


def test_link_info_traces_import_to_c_source_and_project(s):
    from lal_mcp.tools.link import link_info

    text, err = link_info(s, "Bit_Ops", True)
    assert not err
    lines = text.splitlines()
    assert lines[0] == '[status: INCOMPLETE: unresolved symbol "hal_missing_symbol"]'
    assert "foreign imports/exports (3):" in text  # pragma imports are not double-counted as aspects
    assert 'Bit_Shift_Left  import, convention C (aspect), symbol "ibslft_"' in text
    assert "defined: hal/src/hal_bits.c:4 function definition, project Hal (hal/hal.gpr)" in text
    assert 'Hal_Reset  import, convention C (pragma), symbol "hal_reset"' in text
    assert 'unresolved: no definition of "hal_missing_symbol"' in text
    assert "Hal  hal/hal.gpr  library" in text and "withed by root: direct" in text
    assert "linker options: -lm" in text
    assert "runtime units (GNAT runtime, linked automatically): Interfaces" in text


def test_link_info_missing_body(s):
    from lal_mcp.tools.link import link_info

    text, _ = link_info(s, "Orphan", True)
    assert text.splitlines()[0] == "[status: INCOMPLETE: missing body for Do_Work]"
    assert "app/src/orphan.ads:3  Do_Work: no body in the project" in text


def test_link_info_complete_with_runtime_dependency(s):
    from lal_mcp.tools.link import link_info

    text, _ = link_info(s, "Clock_User", True)
    assert text.splitlines()[0] == "[status: complete]"
    assert "Ada.Calendar" in text


def test_link_info_unknown_unit(s):
    from lal_mcp.tools.link import link_info

    with pytest.raises(ToolFailure, match="unresolved: no unit named Nope"):
        link_info(s, "Nope", True)


# --------------------------------------------------------------- test_model


def _model(s, line, file="app/src/bit_ops.ads", column=0):
    from lal_mcp.tools.testmodel import test_model

    text, err = test_model(s, file, line, column)
    assert not err
    return text


def test_model_modular_ranges_contracts_branches_state(s):
    t = _model(s, 14)  # Set_Bit
    assert t.startswith("[status: complete]")
    assert "parts: SubpDecl app/src/bit_ops.ads:14, SubpBody app/src/bit_ops.adb:5" in t
    assert "Pre => Bit <= 15  (app/src/bit_ops.ads:15)" in t
    assert "modular: mod 65536 (16 bits" in t and "range 0 .. 65535" in t
    assert "boundary values: 0, 1, 32768, 65534, 65535" in t
    assert "type Bit_Index (app/src/bit_ops.ads:7): signed integer; range 0 .. 15" in t
    assert "app/src/bit_ops.adb:8  if Bit > 15" in t and "app/src/bit_ops.adb:10  elsif W = Word'Last" in t
    assert 'app/src/bit_ops.adb:9  raise Bit_Error with "bit index out of range";' in t
    assert "writes Bit_Ops.Calls (app/src/bit_ops.ads:11)" in t
    assert "derive from the specification/contract" in t  # never offers oracle values


def test_model_foreign_callees_are_double_candidates(s):
    t = _model(s, 17)  # Shift
    assert "Hal.Bit_Shift_Left  [foreign import (link_info: symbol and owner), other unit]" in t
    assert "Hal.Hal_Reset  [foreign import" in t


def test_model_bit_operations(s):
    t = _model(s, 20)  # Rotate
    assert "Interfaces.Rotate_Left (shift/rotate" in t
    assert "conversion Word -> Unsigned_16" in t and "conversion Unsigned_16 -> Word" in t


def test_model_dispatching_classwide_and_handlers(s):
    t = _model(s, 25)  # Checked_Read
    assert "null record (no components); tagged, abstract; class-wide" in t
    assert "Bit_Ops.Read  [dispatching (test each overriding)]" in t
    assert "case V: when 0 | others" in t
    assert "handler: when Bit_Error" in t


def test_model_imported_and_missing_bodies(s):
    t = _model(s, 7, file="hal/src/hal.ads")
    assert t.startswith("[status: complete]") and "body: none, imported (foreign code)" in t
    t = _model(s, 3, file="app/src/orphan.ads")
    assert t.startswith("[status: INCOMPLETE: missing body]")


def test_model_whole_file_shares_types_and_aliases_paths(s):
    t = _model(s, 0)
    assert t.startswith("[status: complete]\ntest models: 5 subprograms in app/src/bit_ops.ads")
    assert "legend: paths: S = app/src/bit_ops.ads, B = app/src/bit_ops.adb" in t
    # each type once, referred to by id from every parameter and result
    assert t.count("modular: mod 65536") == 1 and "  W : in Word  [T1]" in t and "result: Word  [T1]" in t
    assert "boundary values: 0, 1, 32768, 65534, 65535" in t
    for n, name in enumerate(["Set_Bit S:14", "Shift S:17", "Rotate S:20", "Read S:23", "Checked_Read S:25"], 1):
        assert f"--- {n}/5 {name} " in t
    assert "B:8  if Bit > 15" in t and "Pre => Bit <= 15  (S:15)" in t
    assert "Hal.Bit_Shift_Left  [foreign import" in t  # same facts as the per-subprogram model
    assert "derive from the specification/contract" in t


def test_model_whole_file_pages_at_whole_blocks(s):
    from lal_mcp.tools.testmodel import test_model

    old = s.cfg.max_chars
    s.cfg.max_chars = 2500
    try:
        seen, col = [], 1
        while True:
            t, _ = test_model(s, "app/src/bit_ops.ads", 0, col)
            assert len(t) <= 2500
            seen += [ln.split()[2] for ln in t.splitlines() if ln.startswith("--- ")]
            first = t.splitlines()[0]
            if "column=" not in first:
                break
            assert first.startswith("[status: complete] subprograms ")
            col = int(first.rsplit("column=", 1)[1])
        assert seen == ["Set_Bit", "Shift", "Rotate", "Read", "Checked_Read"]
    finally:
        s.cfg.max_chars = old
    with pytest.raises(ToolFailure, match="declares 5 subprograms"):
        test_model(s, "app/src/bit_ops.ads", 0, 9)


def test_model_needs_a_subprogram_line(s):
    from lal_mcp.tools.testmodel import test_model

    with pytest.raises(ToolFailure, match="no subprogram declared at app/src/bit_ops.ads:6"):
        test_model(s, "app/src/bit_ops.ads", 6, 0)


def test_link_info_aggregate_root_counts_aggregated_projects(tmp_path):
    """With an aggregate root (like a Master.gpr), the aggregated projects are part of the build:
    no "not withed by the root project" gap for them or for what they with."""
    from lal_mcp.tools.link import link_info

    app = os.path.join(LINK_FIXTURE, "app", "app.gpr").replace("\\", "/")
    agg = tmp_path / "everything.gpr"
    agg.write_text(f'aggregate project Everything is\n   for Project_Files use ("{app}");\nend Everything;\n')
    sess = Session(Config(project=str(agg), workspace=LINK_FIXTURE, gnattest=False))
    sess.ensure_loaded()
    text, err = link_info(sess, "Bit_Ops", True)
    assert not err
    assert text.splitlines()[0] == '[status: INCOMPLETE: unresolved symbol "hal_missing_symbol"]'
    assert "not withed by the root project" not in text
    assert "withed by root: aggregated by the root" in text  # App
    assert "withed by root: indirect" in text  # Hal, withed by App


def test_link_info_deps_include_what_subunits_with(tmp_path):
    """A unit whose only path to a C import is a `separate` body's with clause: link_info
    with dependencies must still list the import and its owner."""
    from lal_mcp.tools.link import link_info

    w = tmp_path / "fx"
    shutil.copytree(LINK_FIXTURE, w, ignore=shutil.ignore_patterns("obj", "lib"))
    src = w / "app" / "src"
    (src / "sub_user.ads").write_text("package Sub_User is\n   procedure Run;\nend Sub_User;\n")
    (src / "sub_user.adb").write_text("package body Sub_User is\n   procedure Run is separate;\nend Sub_User;\n")
    (src / "sub_user-run.adb").write_text(
        "with Hal;\nseparate (Sub_User)\nprocedure Run is\nbegin\n   Hal.Hal_Reset;\nend Run;\n")
    sess = Session(Config(project=str(w / "app" / "app.gpr"), workspace=str(w), gnattest=False))
    sess.ensure_loaded()
    text, err = link_info(sess, "Sub_User", True)
    assert not err
    assert 'Hal_Reset  import, convention C (pragma), symbol "hal_reset"' in text
    assert "project Hal (hal/hal.gpr)" in text


REAL_TYPES = {
    "real.gpr": 'project Real is\n   for Source_Dirs use ("src");\n   for Object_Dir use "obj";\nend Real;\n',
    "src/consts.ads": "package Consts is\n"
                      "   PI       : constant := 3.14159_26535_89793;\n"
                      "   THREE_PI : constant := 3.0 * PI;\n"
                      "end Consts;\n",
    "src/types.ads": "with Consts;\n"
                     "package Types is\n"
                     "   package UC renames Consts;\n"
                     "   type Radians is digits 6 range -UC.THREE_PI .. UC.THREE_PI;\n"
                     "   subtype Half is Radians range -UC.PI .. UC.PI;\n"
                     "   type Bams is delta 2.0**(-15) range -1.0 .. 1.0 - 2.0**(-15);\n"
                     "   for Bams'Small use 2.0**(-15);\n"
                     "   for Bams'Size use 16;\n"
                     "end Types;\n",
    "src/conv.ads": "with Types;\n"
                    "package Conv is\n"
                    "   package T renames Types;\n"
                    "   function To_Bams (X : T.Radians) return T.Bams;\n"
                    "   function Halve (X : T.Half) return T.Half;\n"
                    "end Conv;\n",
    "src/conv.adb": "with Consts;\n"
                    "package body Conv is\n"
                    "   package C renames Consts;\n"
                    "   function To_Bams (X : T.Radians) return T.Bams is\n"
                    "   begin\n      return T.Bams (X / T.Radians (C.THREE_PI));\n   end To_Bams;\n"
                    "   function Halve (X : T.Half) return T.Half is\n"
                    "   begin\n      return X / 2.0;\n   end Halve;\n"
                    "end Conv;\n",
}


def test_model_real_types_resolve_ranges_through_renamings(tmp_path):
    """Floating and fixed point ranges (none were shown before), with the constants the bounds name
    resolved through package renamings (T.Radians -> Types, UC.THREE_PI -> Consts)."""
    from lal_mcp.tools.testmodel import test_model

    for rel_path, text in REAL_TYPES.items():
        p = tmp_path / rel_path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="latin-1")
    sess = Session(Config(project=str(tmp_path / "real.gpr"), workspace=str(tmp_path), gnattest=False))
    sess.ensure_loaded()
    t, _ = test_model(sess, "src/conv.ads", 4, 0)
    assert t.startswith("[status: complete]"), t
    assert "type Radians (src/types.ads:4): floating point, digits 6; " \
           "range -UC.THREE_PI .. UC.THREE_PI (~ -9.42477796076938 .. 9.42477796076938)" in t
    assert "UC.THREE_PI = Consts.THREE_PI := 3.0 * PI (~ 9.42477796076938) [named number, src/consts.ads:3]" in t
    assert "PI = Consts.PI := 3.14159_26535_89793" in t  # followed into THREE_PI's definition
    assert "boundary values: Radians'First, 0.0, Radians'Last" in t
    assert "fixed point, delta 2.0**(-15); Small => 2.0**(-15) (= 3.0517578125e-05); Size => 16; " \
           "range -1.0 .. 1.0 - 2.0**(-15) (~ -1 .. 0.999969482421875)" in t
    t, _ = test_model(sess, "src/conv.ads", 0, 0)
    assert t.startswith("[status: complete]"), t
    # a subtype's own range constraint wins over its type's range
    assert "Half (src/types.ads:5): floating point, digits 6; range -UC.PI .. UC.PI (~ -3.14159265358979" in t


def test_model_visibility_abbreviations_and_context(tmp_path):
    """Which package abbreviations a GNATtest test (a child of the spec) can use, and the exact
    with / use type lines for the parameter and result types, in full names."""
    from lal_mcp.tools.testmodel import test_model

    for rel_path, text in REAL_TYPES.items():
        p = tmp_path / rel_path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="latin-1")
    sess = Session(Config(project=str(tmp_path / "real.gpr"), workspace=str(tmp_path), gnattest=False))
    sess.ensure_loaded()
    t, _ = test_model(sess, "src/conv.ads", 0, 0)
    assert "  T = Types [S:3, spec: usable in test bodies as T.X; in @context write Types]" in t, t
    assert "  C = Consts [B:3, body only: NOT visible in tests; write Consts.X and `with Consts;`]" in t
    ctx = t.split("test context (", 1)[1].splitlines()
    assert ctx[1:4] == ["  with Types;", "  use type Types.Radians;", "  use type Types.Bams;"], ctx[:6]
    assert "  use type Types.Half;" in ctx  # a subtype: its operators are the type's
    assert "use type T." not in t  # never the abbreviation in a context clause
    t, _ = test_model(sess, "src/conv.ads", 4, 0)  # the single-subprogram model carries it too
    assert "  with Types;" in t and "  use type Types.Radians;" in t and "Types.Half" not in t


def test_model_context_skips_types_of_the_package_itself(s):
    t = _model(s, 17)  # Shift: Interfaces.Unsigned_32 from the runtime, Natural from Standard
    assert "  with Interfaces;\n  use type Interfaces.Unsigned_32;" in t
    t = _model(s, 14)  # Set_Bit: Word and Bit_Index are declared in Bit_Ops itself
    assert "test context: none needed" in t
