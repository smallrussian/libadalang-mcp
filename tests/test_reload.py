"""Project reload/invalidation: files added, removed, re-added; .gpr edits; atomic saves."""

from __future__ import annotations

import os
import shutil
import time

import anyio
import pytest

from conftest import FIXTURE, mcp_client, requires_lal
from lal_mcp.session import Config, Session, ToolFailure

pytestmark = requires_lal


@pytest.fixture()
def fx(tmp_path):
    work = tmp_path / "fx"
    shutil.copytree(FIXTURE, work, ignore=shutil.ignore_patterns("obj"))
    s = Session(Config(project=str(work / "fixture.gpr")))
    s.ensure_loaded()
    return s, work


def _bump(path):
    """Make sure a directory/file timestamp moves even on coarse-resolution filesystems."""
    st = os.stat(path)
    os.utime(path, (st.st_atime, st.st_mtime + 2))


def _refresh(s):
    return s.refresh_if_changed(interval=0)


def _units(s):
    from lal_mcp.tools import units

    return units


def test_no_change_no_reload(fx):
    s, _ = fx
    assert _refresh(s) is None and s.reloads == 0


def test_added_file_is_picked_up(fx):
    s, work = fx
    units = _units(s)
    with pytest.raises(ToolFailure, match="no unit named New_Unit"):
        units.find_unit(s, "New_Unit")
    (work / "src" / "new_unit.1.ada").write_text("package New_Unit is\n   X : Integer := 1;\nend New_Unit;\n")
    _bump(work / "src")
    note = _refresh(s)
    assert note and "1 file(s) added in src" in note and "(+1 / -0)" in note
    text, _ = units.find_unit(s, "New_Unit")
    assert "spec     src/new_unit.1.ada" in text


def test_removed_then_readded_file(fx):
    s, work = fx
    units = _units(s)
    f = work / "src" / "garage.2.ada"
    body = f.read_text()
    f.unlink()
    _bump(work / "src")
    note = _refresh(s)
    assert note and "1 file(s) removed from src" in note and "(+0 / -1)" in note
    text, _ = units.find_unit(s, "Garage")
    assert "body     none in project" in text
    f.write_text(body)  # re-added: the case a mtime-only check misses for a cached source list
    _bump(work / "src")
    note = _refresh(s)
    assert note and "1 file(s) added in src" in note
    text, _ = units.find_unit(s, "Garage")
    assert "body     src/garage.2.ada" in text


def test_atomic_save_and_content_edit_do_not_reload(fx):
    s, work = fx
    f = work / "src" / "vehicle.1.ada"
    tmp = work / "src" / "vehicle.1.ada.tmp~"
    tmp.write_text(f.read_text().replace(":= 300", ":= 299"))
    os.replace(tmp, f)  # write temp + rename over: bumps the directory mtime
    _bump(work / "src")
    assert _refresh(s) is None and s.reloads == 0
    assert "Max_Speed : constant := 299" in s.unit(str(f)).text  # content still re-read by mtime


def test_gpr_edit_reloads_and_new_child_reaches_derived_caches(fx):
    s, work = fx
    units = _units(s)
    units.package_files(s, "Vehicle", True, False, False)  # fills the unit index cache
    (work / "src" / "vehicle__extra.1.ada").write_text("package Vehicle.Extra is\nend Vehicle.Extra;\n")
    gpr = work / "fixture.gpr"
    gpr.write_text(gpr.read_text().replace("--  Test fixture", "--  Test fixture (edited)"))
    _bump(gpr)
    note = _refresh(s)
    assert note and "fixture.gpr changed" in note
    text, _ = units.package_files(s, "Vehicle", True, False, False)
    assert "Vehicle.Extra" in text and "vehicle__extra.1.ada" in text


def test_broken_gpr_keeps_previous_view_then_recovers(fx):
    s, work = fx
    units = _units(s)
    gpr = work / "fixture.gpr"
    good = gpr.read_text()
    gpr.write_text(good.replace("end Fixture;", "end Fixture"))  # syntax error
    _bump(gpr)
    with pytest.raises(ToolFailure, match="project reload failed .*previous project view"):
        _refresh(s)
    text, _ = units.find_unit(s, "Vehicle")  # still answers from the old view
    assert "spec     src/vehicle.1.ada" in text
    gpr.write_text(good)
    _bump(gpr)
    note = _refresh(s)
    assert note and "fixture.gpr changed" in note


def test_reload_project_tool(fx):
    from lal_mcp.tools.project import reload_project

    s, _ = fx
    text, err = reload_project(s)
    assert not err and text.startswith("[project reloaded: requested; 12 sources (+0 / -0)]")
    assert "watching 1 project file(s)" in text


def test_reload_note_over_stdio(tmp_path):
    work = tmp_path / "fx"
    shutil.copytree(FIXTURE, work, ignore=shutil.ignore_patterns("obj"))

    async def go():
        async with mcp_client(["-P", str(work / "fixture.gpr")]) as (s, _):
            r1 = await s.call_tool("find_unit", {"name": "Late_Unit"})
            (work / "src" / "late_unit.1.ada").write_text("package Late_Unit is\nend Late_Unit;\n")
            _bump(work / "src")
            await anyio.sleep(1.2)  # the automatic check runs at most once a second
            r2 = await s.call_tool("find_unit", {"name": "Late_Unit"})
            return r1, r2

    r1, r2 = anyio.run(go)
    assert r1.is_error and "no unit named Late_Unit" in r1.content[0].text
    text = r2.content[0].text
    assert not r2.is_error, text
    assert text.startswith("[project reloaded: 1 file(s) added in src") and "late_unit.1.ada" in text
