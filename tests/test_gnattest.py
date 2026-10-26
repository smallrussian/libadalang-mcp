"""GNATtest (GNATdas) harness: auto-detected, loaded as the root, picked up when generated later,
reported by reload_project and test_model. Needs libadalang plus gnattest/gprinspect on PATH."""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from conftest import TESTS, requires_lal
from lal_mcp.session import Config, Session

LINK_FIXTURE = os.path.join(TESTS, "fixture_link")

pytestmark = [requires_lal,
              pytest.mark.skipif(not (shutil.which("gnattest") and shutil.which("gprinspect")),
                                 reason="gnattest/gprinspect (GNAT Pro bin) not on PATH")]


@pytest.fixture()
def work(tmp_path):
    w = tmp_path / "fx"
    shutil.copytree(LINK_FIXTURE, w, ignore=shutil.ignore_patterns("obj", "lib"))
    return w


def _session(w, **kw) -> Session:
    s = Session(Config(project=str(w / "app" / "app.gpr"), workspace=str(w), **kw))
    s.ensure_loaded()
    return s


def _gnattest(w):
    r = subprocess.run(["gnattest", "-P", str(w / "app" / "app.gpr"), "bit_ops.ads", "-q"],
                       cwd=w, capture_output=True, text=True, timeout=300,
                       stdin=subprocess.DEVNULL)  # an inherited stdin is invalid once libadalang is loaded (Windows)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (w / "app" / "obj" / "gnattest" / "harness" / "test_driver.gpr").is_file()


def test_harness_generated_later_is_picked_up(work):
    from lal_mcp.tools import testmodel, units

    s = _session(work)
    assert s.harness is None
    assert s.harness_note == "no GNATtest harness yet (expected at app/obj/gnattest/harness/test_driver.gpr)"
    _gnattest(work)
    note = s.refresh_if_changed(interval=0)
    assert note and "test_driver.gpr created" in note and "GNATtest harness app/obj/gnattest/harness" in note
    text, _ = units.find_unit(s, "Bit_Ops.Test_Data.Tests")
    assert "body     app/obj/gnattest/tests/bit_ops-test_data-tests.adb" in text
    t, _ = testmodel.test_model(s, "app/src/bit_ops.ads", 14, 0)
    assert "gnattest test: app/obj/gnattest/tests/bit_ops-test_data-tests.adb:" in t
    assert "Test_Set_Bit (generated skeleton, not implemented yet" in t


def test_filled_in_test_is_reported_implemented(work):
    from lal_mcp.tools import testmodel

    _gnattest(work)
    s = _session(work)
    tests = work / "app" / "obj" / "gnattest" / "tests" / "bit_ops-test_data-tests.adb"
    text = tests.read_text(encoding="latin-1")
    start = text.index("--  bit_ops.ads:14:4:Set_Bit")
    end = text.index("end Test_Set_Bit;", start)
    body = text[start:end].replace('"Test not implemented."', '"Set_Bit (0, 0) = 1"').replace(
        "Gnattest_Generated.Default_Assert_Value", "Set_Bit (0, 0) = 1")
    tests.write_text(text[:start] + body + text[end:], encoding="latin-1")
    t, _ = testmodel.test_model(s, "app/src/bit_ops.ads", 14, 0)
    assert "Test_Set_Bit (implemented)" in t


def test_reload_project_lists_harness_tests(work):
    from lal_mcp.tools.project import reload_project

    _gnattest(work)
    s = _session(work)
    text, err = reload_project(s)
    assert not err
    assert "GNATtest harness app/obj/gnattest/harness/test_driver.gpr" in text.splitlines()[0]
    assert "1 test package(s):" in text and "app/obj/gnattest/tests/bit_ops-test_data-tests.adb" in text


def test_gnattest_detection_can_be_turned_off(work):
    _gnattest(work)
    s = _session(work, gnattest=False)
    assert s.harness is None and not any("obj/gnattest/" in s.rel(f) for f in s.sources())


def test_broken_harness_falls_back_to_project(work):
    _gnattest(work)
    drv = work / "app" / "obj" / "gnattest" / "harness" / "test_driver.gpr"
    drv.write_text("project Test_Driver is\n", encoding="latin-1")  # truncated: syntax error
    s = _session(work)
    assert s.harness is None
    assert s.harness_note and s.harness_note.startswith(
        "GNATtest harness app/obj/gnattest/harness/test_driver.gpr not loaded")
    assert any(f.endswith("bit_ops.ads") for f in s.sources())  # the project itself still works
