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


HARNESS = ("app", "obj", "gnattest", "harness")


def _run_gnattest(w, *args):
    r = subprocess.run(["gnattest", "-P", str(w / "app" / "app.gpr"), *args, "-q"],
                       cwd=w, capture_output=True, text=True, timeout=300,
                       stdin=subprocess.DEVNULL)  # an inherited stdin is invalid once libadalang is loaded (Windows)
    assert r.returncode == 0, r.stdout + r.stderr


def _gnattest(w):
    _run_gnattest(w, "bit_ops.ads")
    assert w.joinpath(*HARNESS, "test_driver.gpr").is_file()


def _gnattest_separate(w, per="unit", *extra):
    # gnattest 26.1 applies --separate-drivers only without source file arguments
    _run_gnattest(w, f"--separate-drivers={per}", *extra)
    assert w.joinpath(*HARNESS, "test_drivers.gpr").is_file()
    assert not w.joinpath(*HARNESS, "test_driver.gpr").exists()


def test_harness_generated_later_is_picked_up(work):
    from lal_mcp.tools import testmodel, units

    s = _session(work)
    assert s.harness is None
    assert s.harness_note == ("no GNATtest harness yet (expected at app/obj/gnattest/harness/test_driver.gpr, "
                              "or test_drivers.gpr for --separate-drivers)")
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


# ------------------------------------------------------------ --separate-drivers


def test_separate_drivers_harness_generated_later_is_picked_up(work):
    from lal_mcp.tools import navigate, units

    s = _session(work)
    assert s.harness is None
    _gnattest_separate(work)
    note = s.refresh_if_changed(interval=0)
    assert note and "test_drivers.gpr created" in note
    assert "GNATtest harness app/obj/gnattest/harness/test_drivers.gpr (separate drivers: 3)" in note
    assert s.harness_mode == "separate"
    text, _ = units.find_unit(s, "Bit_Ops.Test_Data.Tests")
    assert "body     app/obj/gnattest/tests/bit_ops-test_data-tests.adb" in text
    text, _ = units.find_unit(s, "Bit_Ops.Test_Data.Tests.Suite")
    assert "app/obj/gnattest/harness/Bit_Ops.Test_Data.Tests/bit_ops-test_data-tests-suite.adb" in text
    # the code under test still resolves through the harness view
    src = (work / "app" / "src" / "bit_ops.adb").read_text(encoding="latin-1").splitlines()
    line = next(i for i, ln in enumerate(src, 1) if "Bit_Shift_Left" in ln and "--" not in ln.split("Bit_Shift_Left")[0])
    col = src[line - 1].index("Bit_Shift_Left") + 1
    text, err = navigate.goto_definition(s, "app/src/bit_ops.adb", line, col)
    assert not err and "hal/src/hal.ads:" in text


def test_reload_project_lists_separate_drivers(work):
    from lal_mcp.tools.project import reload_project

    _gnattest_separate(work)
    s = _session(work)
    text, err = reload_project(s)
    assert not err
    assert "GNATtest harness (separate drivers): app/obj/gnattest/harness/test_drivers.gpr" in text
    assert "3 driver(s)" in text
    assert ("app/obj/gnattest/harness/Bit_Ops.Test_Data.Tests/test_driver.gpr: units bit_ops; runner "
            "app/obj/gnattest/harness/Bit_Ops.Test_Data.Tests/bit_ops-test_data-tests-suite-test_runner") in text
    assert "(not built)" in text


def test_test_model_names_the_driver_per_unit(work):
    from lal_mcp.tools import testmodel

    _gnattest_separate(work)
    s = _session(work)
    t, _ = testmodel.test_model(s, "app/src/bit_ops.ads", 14, 0)
    assert "gnattest test: app/obj/gnattest/tests/bit_ops-test_data-tests.adb:" in t and "Test_Set_Bit (" in t
    assert ("gnattest driver: app/obj/gnattest/harness/Bit_Ops.Test_Data.Tests/test_driver.gpr; runner "
            "app/obj/gnattest/harness/Bit_Ops.Test_Data.Tests/bit_ops-test_data-tests-suite-test_runner") in t


def test_test_model_names_the_driver_per_test(work):
    from lal_mcp.tools import testmodel

    _gnattest_separate(work, "test")
    s = _session(work)
    assert len(s.harness_drivers()) > 3  # one driver per test
    t, _ = testmodel.test_model(s, "app/src/bit_ops.ads", 14, 0)
    drv = [ln for ln in t.splitlines() if ln.startswith("gnattest driver:")]
    assert len(drv) == 1, t  # exactly the Set_Bit driver, not every driver of the unit
    assert "Bit_Ops.Test_Data.Tests/driver_test_set_bit_" in drv[0]
    assert "runner app/obj/gnattest/harness/Bit_Ops.Test_Data.Tests/bit_ops-test_data-tests-driver_test_set_bit_" in drv[0]


def test_both_layouts_present_loads_the_newer(work):
    _gnattest(work)  # single driver first ...
    single = work.joinpath(*HARNESS, "test_driver.gpr")
    _run_gnattest(work, "--separate-drivers=unit")  # ... then separate drivers on top
    sep = work.joinpath(*HARNESS, "test_drivers.gpr")
    assert single.is_file() and sep.is_file()
    t = single.stat().st_mtime
    os.utime(sep, (t + 10, t + 10))
    s = _session(work)
    assert s.harness.endswith("test_drivers.gpr")
    assert "loaded the newer test_drivers.gpr" in s.harness_choice
    os.utime(single, (t + 20, t + 20))
    note = s.refresh_if_changed(interval=0)
    assert note and "test_driver.gpr changed" in note and s.harness.endswith("test_driver.gpr")


def test_broken_separate_harness_falls_back_to_project(work):
    _gnattest_separate(work)
    work.joinpath(*HARNESS, "test_drivers.gpr").write_text("aggregate project Test_Drivers is\n", encoding="latin-1")
    s = _session(work)
    assert s.harness is None
    assert s.harness_note.startswith("GNATtest harness app/obj/gnattest/harness/test_drivers.gpr not loaded")
    assert any(f.endswith("bit_ops.ads") for f in s.sources())


def test_tests_outside_obj_with_tests_root(work):
    from lal_mcp.tools import testmodel

    _gnattest_separate(work, "unit", f"--tests-root={work / 'test' / 'gnattest'}")
    s = _session(work)
    assert s.harness_mode == "separate"
    t, _ = testmodel.test_model(s, "app/src/bit_ops.ads", 14, 0)
    assert "gnattest test: test/gnattest/app/src/bit_ops-test_data-tests.adb:" in t
    assert "gnattest driver: app/obj/gnattest/harness/Bit_Ops.Test_Data.Tests/test_driver.gpr" in t


def test_explicit_tests_project_directory(work):
    _gnattest_separate(work)
    s = _session(work, tests_project=str(work.joinpath(*HARNESS)), gnattest=False)
    assert s.harness and s.harness.endswith("test_drivers.gpr")
    assert sorted(os.path.basename(w) for w in s._harness_watch) == ["test_driver.gpr", "test_drivers.gpr"]
