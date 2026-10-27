# Task: support GNATtest `--separate-drivers` harnesses in lal-mcp

## Goal

lal-mcp loads a GNATtest (GNATdas) harness so that the test packages resolve
like any other unit, and so that `test_model` can point at each subprogram's
generated test. Today this works only for the default single-driver harness.

Harnesses generated with `gnattest --separate-drivers` must work the same way.
They have one test driver per unit (or per test), and this is a hard
requirement.

Read `CLAUDE.md` first. Its rules apply here:

- read-only server;
- plain, capped answers with `path:line`;
- never write `.gpr` files;
- small commits that are pushed right away.

## What gnattest generates

Generate a separate-drivers harness like this:

```
gnattest -P app/app.gpr --separate-drivers=unit
```

In GNAT Pro 26.1, `--separate-drivers` only takes effect when **no source file
arguments** are given. With a file argument (`gnattest -P app/app.gpr bit_ops.ads
--separate-drivers=unit`), gnattest silently writes the single-driver layout.

The layout below is from `tests/fixture_link`. The harness lives in `app/obj/gnattest/harness/`:

```
test_drivers.gpr          aggregate project: one test_driver.gpr per unit
test_drivers.list         absolute path of every driver's runner executable, one per line
test_app.gpr              shared: withs aunit, gnattest_common, ../../../app.gpr;
                          Source_Dirs = ../tests, <dep>/obj/gnattest/tests, common
gnattest_common.gpr, common/, gnattest.xml, Makefile, ...
Bit_Ops.Test_Data.Tests/
  test_driver.gpr         withs aunit, ../test_app.gpr, gnattest_common; Source_Dirs ".";
                          Main = bit_ops-test_data-tests-suite-test_runner.adb
  bit_ops-test_data-tests-suite.ads/.adb
  bit_ops-test_data-tests-suite-test_runner.adb
  units.list              the unit(s) this driver covers ("bit_ops")
Hal.Test_Data.Tests/  ...
Orphan.Test_Data.Tests/  ...
```

There is **no** top-level `test_driver.gpr` in this mode. That file is what we
detect today, so this layout is missed.

`test_drivers.gpr` (generated, verbatim):

```ada
with "gnattest_common.gpr";

aggregate project Test_Drivers is
   for Project_Files use
     ("Bit_Ops.Test_Data.Tests\test_driver.gpr",
      "Hal.Test_Data.Tests\test_driver.gpr",
      "Orphan.Test_Data.Tests\test_driver.gpr");
   for Origin_Project use "..\..\..\app.gpr";
   ...
end Test_Drivers;
```

## Directory guide: what gnattest writes and where it should go

gnattest writes two separate trees. They have very different owners.

### Tests: authored by people (or agents), kept by gnattest

There is one set per unit. By default each set goes in **its own project's**
object directory. In the fixture, `Hal`'s tests went to `hal/obj/gnattest/tests/`,
not `app/obj/`:

```
<project obj>/gnattest/tests/
  bit_ops-test_data.ads/.adb              Test_Data: fixture type, Set_Up / Tear_Down. Created once, then yours.
  bit_ops-test_data-tests.ads             test declarations (gnattest maintains this)
  bit_ops-test_data-tests.adb             THE TEST BODIES. Write between "--  end read only"
                                          and "--  begin read only". Never edit the read-only regions
                                          or the "--  id:" / "--  <spec>:<line>:<col>:<Name>" markers.
  bit_ops-sensor_test_data[-sensor_tests].ads/.adb   same, per tagged type (inherited-test packages)
```

Regenerating keeps the bodies and `Test_Data`. It only adds skeletons for new
subprograms. This is **source code** and belongs in version control.

### Harness: generated, safe to regenerate

The harness is always under the root project's object directory:

```
<root obj>/gnattest/harness/
  test_drivers.gpr, test_drivers.list         separate drivers (aggregate + list of runner exes)
  test_driver.gpr                             single driver (instead of the two above)
  test_app.gpr                                shared project: withs the project, Source_Dirs = the tests dirs
  <Unit>.Test_Data.Tests/                     one per driver: test_driver.gpr, suite, test_runner, units.list
  common/, gnattest_common.gpr, gnattest.xml, Makefile, *.adc, test_obj/, Test_Driver_obj/   support and build output
```

gnattest overwrites it every run. Don't hand-edit it, and don't commit it.

### Recommendation

Keep authored tests **out of `obj/`**. `obj/` is usually git-ignored and gets
wiped by cleans. Leave the harness in `obj/`. Do this with gnattest switches,
**not** by editing the repository's `.gpr` files. Verified on the fixture:

```
gnattest -P app/app.gpr --separate-drivers=unit --tests-root=<repo>/test/gnattest
```

- **Tests:** they go to `test/gnattest/<mirror of each source dir>/`, for example
  `test/gnattest/app/src/bit_ops-test_data-tests.adb` and
  `test/gnattest/hal/src/hal-test_data-tests.adb`. Commit these.
- **Harness:** it stays in `app/obj/gnattest/harness/`. Its `test_app.gpr` points
  `Source_Dirs` at the new test dirs.
- **Libadalang:** it loads this with no errors, `Bit_Ops.Test_Data.Tests`
  resolves to the new location, and resolution into `hal.ads` still works.
- **Regenerating:** always pass the **same** `--tests-root` (and the same mode).
  Otherwise gnattest creates a fresh set of skeletons elsewhere, and the
  harness stops pointing at the authored tests.

Other location switches are `--tests-dir` (one directory), `--subdirs` (a subdir
of each source dir), `--harness-dir` and `--stubs-dir`. If `--harness-dir`
moves the harness away from `<obj>/gnattest/harness`, auto-detection can't
see it. Set `tests_project = ".../test_drivers.gpr"` in `.lal-mcp.toml`.

The server never hardcodes the tests location: test packages are found
through the loaded harness's `Source_Dirs`. Keep it that way.

## Already verified: Libadalang loads the aggregate

`lal.GPRProject("app/obj/gnattest/harness/test_drivers.gpr")` loads it with no
errors and gives a working context:

- 31 sources, including all three `*-test_data-tests.adb` packages and all
  three `*-suite-test_runner.adb` runners.
- `get_from_provider` finds `Bit_Ops` (spec and body), `Bit_Ops.Test_Data.Tests`
  (spec and body, under `obj/gnattest/tests/`), `Bit_Ops.Test_Data.Tests.Suite`
  (under `harness/Bit_Ops.Test_Data.Tests/`), and `Hal` (spec only, as expected).
- Name resolution from the code under test still works: `Bit_Shift_Left` in
  `app/src/bit_ops.adb` resolves to `hal/src/hal.ads:7`.
- There are no unit-name clashes, because each driver's suite and runner is
  named after its unit.

**So load the aggregate gnattest already wrote. Do not generate one.** This
keeps the "never write `.gpr` files" rule.

## Changes

### 1. Detection: `lal_mcp/buildinfo.py`, `lal_mcp/session.py`

- `BuildInfo.gnattest_harness()` currently returns
  `<object dir>/<Harness_Dir or gnattest/harness>/test_driver.gpr`. Make it
  check the same directory for:
  - `test_drivers.gpr` (separate drivers);
  - `test_driver.gpr` (single driver).
- Pick the one that exists. If both exist, which happens after switching
  modes, pick the newer by mtime and say which one was picked in the
  load/reload note.
- If neither exists, keep the current "no GNATtest harness yet (expected at
  ...)" note, naming both file names.
- `Session._harness_candidate()` / `_build_view()`:
  - The fallback to the plain project when the harness fails to load stays as
    it is.
  - `tests_project` (`.lal-mcp.toml` / `--tests-project`) must accept either
    file. Update the comment on `Config.tests_project`, and the example in
    `docs/lal-mcp.example.toml`.
- Record the mode on the session, e.g. `sess.harness_mode = "single" | "separate"`.

### 2. Watching and reload: `session.py`

- `_harness_watch` must contain **both** candidate paths, so that switching
  between modes, or generating either one later, triggers a reload.
- `project_files()` scans `with` / `extends` clauses textually. An aggregate
  lists its projects in `for Project_Files use (...)`, not in `with` clauses.
  Add that attribute to the scan, so every per-driver `test_driver.gpr` (and
  through it `test_app.gpr`) is watched.
  - Paths are relative to the aggregate's directory and use `\` on Windows.
- Adding or removing a driver directory (for example after regenerating for a
  new unit) must give `[project reloaded: ...]` on the next call. The per-driver
  directories are `Source_Dirs` of their projects, so the source-directory
  watch should cover them once those projects are loaded. Check this.

### 3. Reporting: `lal_mcp/tools/project.py`, `lal_mcp/tools/testmodel.py`

- `reload_project`:
  - Keep listing the test packages.
  - In separate mode, also say `GNATtest harness: <rel>/test_drivers.gpr loaded
    (separate drivers: N)`.
  - List each driver as `<driver dir>: units <units.list contents>; runner <rel
    exe path>`, capped like the test list (20 + "... N more").
  - Runner paths come from `test_drivers.list`. On Windows, gnattest writes them
    **lowercased and absolute**. Match them case-insensitively, and print them
    workspace-relative with forward slashes.
- `test_model` / `gnattest_test()`:
  - The `gnattest test: <file>:<line> Test_X (...)` line stays the same.
  - In separate mode, add the runner that executes it: the driver whose
    `units.list` contains the unit (fallback: the directory named
    `<Unit>.Test_Data.Tests`).
  - If no driver covers the unit, say so plainly. Don't guess.
- Keep both outputs within the response cap.

### 4. Docs and agent files

- `README.md`, GNATtest section: delete "Only the default single-driver harness
  is supported, not `--separate-drivers`". Describe both layouts and the
  no-file-argument rule above.
- `docs/agents/skills/ada-test-runner/SKILL.md`, in separate-drivers mode:
  - **Build all drivers:** `gprbuild -P <harness>/test_drivers.gpr`.
  - **Run all:** `gnattest <harness>/test_drivers.list`. Verify that this
    command really runs every listed driver before documenting it. If it
    doesn't, document running each exe from `test_drivers.list`.
  - **Run one unit's tests:** that driver's exe (from `reload_project` /
    `test_model`).
  - Keep the single-driver instructions.
- `docs/agents/skills/ada-unit-tests/SKILL.md`: say where test bodies live.
  They're `*-test_data-tests.adb` in the tests tree, the same in both modes;
  only the drivers differ. Take the path from `test_model`, don't assume
  `obj/`.
- README and runner skill: document the recommended layout from the
  directory guide above (`--tests-root` outside `obj/`, harness in `obj/`, the
  same switches on every regeneration).
- `docs/AGENTS-snippet.md`: one line saying separate-drivers harnesses are
  loaded too.

### 5. Tests: `tests/test_gnattest.py`

These tests need `gnattest` and `gprinspect` on `PATH`. Skip them otherwise,
like the existing ones. Add a helper that runs:

```
gnattest -P app/app.gpr --separate-drivers=unit -q
```

with `stdin=subprocess.DEVNULL`, which is needed on Windows after Libadalang is
loaded in-process. Add tests that:

1. A separate-drivers harness generated after the session starts is picked up:
   the next call's note says `test_drivers.gpr created`, and `sess.harness` ends
   with `test_drivers.gpr`.
2. `Bit_Ops.Test_Data.Tests` and `Bit_Ops.Test_Data.Tests.Suite` resolve, and
   `Bit_Shift_Left` in `bit_ops.adb` still resolves into `hal.ads`.
3. `reload_project` lists 3 drivers, each with its units and runner path.
4. `test_model` on `Set_Bit` in `bit_ops.ads` names the generated test and the
   `Bit_Ops.Test_Data.Tests` runner.
5. Switching modes: when both files exist, the newer one is loaded and the
   choice is reported.
6. A broken `test_drivers.gpr` falls back to the plain project with a note.
7. `--tests-root=<tmp>/test/gnattest`: the tests outside `obj/` resolve, and
   `test_model` points at `test/gnattest/app/src/bit_ops-test_data-tests.adb`.

Run only these tests locally (`pytest tests/test_gnattest.py
tests/test_reload.py`). CI runs the full suite.

## Done when

- A `--separate-drivers` harness is detected, loaded, watched and reported.
- The single-driver behavior and its tests are unchanged.
- Docs and skills are updated, and the README "not supported" note is gone.
- The work is in small conventional commits, each pushed. For example:
  `feat(session): load GNATtest separate-drivers harness`,
  `feat(tools): report per-driver runners`,
  `docs: separate-drivers in README and skills`.
