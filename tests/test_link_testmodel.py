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
