from __future__ import annotations

import os
import shutil

import pytest

from conftest import FIXTURE, FIXTURE_GPR, requires_lal
from lal_mcp.session import Config, Session, ToolFailure, cap

pytestmark = requires_lal


@pytest.fixture(scope="module")
def sess() -> Session:
    s = Session(Config(project=FIXTURE_GPR))
    s.ensure_loaded()
    return s


def test_loads_project_with_naming_package(sess: Session):
    names = sorted(os.path.basename(f) for f in sess.sources())
    assert "vehicle__engine__start.2.ada" in names
    assert "utils_spec.ada" in names
    assert sess.project_errors == []


def test_rel_paths_are_workspace_relative(sess: Session):
    spec = sess.unit_for_name("Vehicle.Engine", body=False)
    assert sess.rel(spec.filename) == "src/vehicle__engine.1.ada"


def test_unit_for_name_missing_part_is_none(sess: Session):
    assert sess.unit_for_name("Main_Proc", body=False) is None
    assert sess.unit_for_name("Main_Proc", body=True) is not None
    assert sess.unit_for_name("No.Such.Unit", body=True) is None


def test_invalid_unit_name_is_tool_failure(sess: Session):
    with pytest.raises(ToolFailure, match="invalid unit name"):
        sess.unit_for_name("not a name!", body=False)


def test_resolve_path_variants(sess: Session):
    a = sess.resolve_path("src/vehicle.1.ada")
    assert sess.resolve_path("vehicle.1.ada") == a  # bare file name found among sources
    assert sess.resolve_path(a) == a
    with pytest.raises(ToolFailure, match="file not found"):
        sess.resolve_path("src/nope.ada")


def test_missing_project_is_tool_failure(tmp_path):
    s = Session(Config(project=str(tmp_path / "nope.gpr")))
    with pytest.raises(ToolFailure, match="project file not found"):
        s.ensure_loaded()
    with pytest.raises(ToolFailure):  # stays failed, no retry storm
        s.ensure_loaded()


def test_reparse_on_mtime_change(tmp_path):
    work = tmp_path / "fx"
    shutil.copytree(FIXTURE, work, ignore=shutil.ignore_patterns("obj"))
    s = Session(Config(project=str(work / "fixture.gpr")))
    s.ensure_loaded()
    path = str(work / "src" / "vehicle.1.ada")
    u = s.unit(path)
    assert "Max_Speed : constant := 300" in u.text
    with open(path, encoding="latin-1") as f:
        text = f.read()
    with open(path, "w", encoding="latin-1") as f:
        f.write(text.replace(":= 300", ":= 250"))
    st = os.stat(path)
    os.utime(path, (st.st_atime, st.st_mtime + 5))
    u2 = s.unit(path)
    assert "Max_Speed : constant := 250" in u2.text


def test_cap_truncates_at_line_and_says_so():
    text = "\n".join(f"line {i}" for i in range(1000))
    out = cap(text, 200, "use a smaller limit")
    assert len(out) <= 200
    assert "[truncated:" in out and "use a smaller limit" in out
    assert cap("short", 200) == "short"
