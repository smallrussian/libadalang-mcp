# Unit testing Ada with GNATtest and lal-mcp

This guide shows how to generate, write, build and run GNATtest (GNATdas) unit
tests in a repository that uses the `ada` MCP server (lal-mcp), and what works
best. It's for the people who set up the repository and for the coding agent
(Codex, Claude) that writes and runs the tests.

All examples are from `tests/fixture_link` (projects `app` and `hal`) with GNAT Pro 26.1.

## The short version

1. Generate once, with the switches you will always use:
   `gnattest -P app.gpr --separate-drivers=unit --tests-root=<repo>/test/gnattest`.
2. The MCP server picks up the harness by itself. The next answer starts with
   `[project reloaded: test_drivers.gpr created; ...]`.
3. For each subprogram:
   - `test_model <spec> <line>` gives the facts to test and the exact test
     procedure to fill in;
   - derive the expected values from the specification, never from the body;
   - write them between the read-only markers.
4. Build with `gprbuild -P obj/gnattest/harness/test_drivers.gpr` and run the
   runner that `test_model` names.
5. Commit the test bodies. Don't commit the harness.

## How it fits together

```
your project (app.gpr)  <-- withed by --  test_app.gpr        (shared: sees all test packages)
                                             ^          ^
                         <Unit>.Test_Data.Tests/test_driver.gpr  (one per unit or per test: suite + runner)
                                             ^
                                   test_drivers.gpr             (aggregate gnattest writes; lal-mcp loads it)
```

The server loads the harness project as the root instead of your project.
Because the harness withs your project, every answer about your code is
unchanged. The generated test packages, suites and runners become sources too,
so they resolve, have outlines, and can be cross-referenced like any unit.

The server never generates or edits `.gpr` files. It only reads the ones
gnattest wrote.

## Driver layouts

| | single driver (default) | `--separate-drivers=unit` | `--separate-drivers=test` |
|---|---|---|---|
| harness project | `test_driver.gpr` | `test_drivers.gpr` (aggregate) | `test_drivers.gpr` (aggregate) |
| drivers | one, `test_runner` | one per unit, in `<Unit>.Test_Data.Tests/` | one per test, `driver_test_<name>_<hash>` |
| one crash kills | every test | that unit's tests | that test only |
| build time | lowest | more | most (one link per test) |
| run a subset | no | one unit | one test |

Choose `=unit` for most repositories. A crash or a `Storage_Error` in one
unit's tests doesn't hide the results of all the others, and you can rerun one
unit after a fix. Choose `=test` when tests share global state and must be
isolated from each other. Choose single driver only for small projects.

In gnattest 26.1, **`--separate-drivers` takes effect only without source file
arguments.** `gnattest -P app.gpr bit_ops.ads --separate-drivers=unit` silently
generates a single driver. Generate for the whole project instead. Regenerating
only adds skeletons for new subprograms, so this costs nothing.

## Directory layout and where things go

gnattest writes two trees with different owners.

### Tests: your code

```
<tests dir>/
  bit_ops-test_data.ads/.adb          Test_Data: fixture type, Set_Up / Tear_Down. Created once, then yours.
  bit_ops-test_data-tests.ads         test declarations (gnattest maintains it)
  bit_ops-test_data-tests.adb         THE TEST BODIES
  bit_ops-sensor_test_data[...]       the same, per tagged type
```

By default the tests dir is **each project's** `<obj>/gnattest/tests/`. For
example, `hal`'s tests go to `hal/obj/gnattest/tests/`, not to `app/obj/`.

### Harness: generated

```
<root obj>/gnattest/harness/
  test_drivers.gpr, test_drivers.list   (separate drivers)  or  test_driver.gpr (single)
  test_app.gpr                          shared project: withs your project, Source_Dirs = the tests dirs
  <Unit>.Test_Data.Tests/               driver project(s), suite, runner(s), units.list
  common/, gnattest_common.gpr, gnattest.xml, Makefile, *.adc, *_obj/   support files and build output
```

gnattest overwrites the harness on every run.

### Recommended layout

- **Keep the tests in the source tree.** Use
  `--tests-root=<repo>/test/gnattest`: the tests then mirror each source
  directory (`test/gnattest/app/src/bit_ops-test_data-tests.adb`,
  `test/gnattest/hal/src/...`). Commit them. In the default location under
  `obj/`, they're usually git-ignored and deleted by a clean.
- **Leave the harness in `obj/`.** Don't edit it, and don't commit it. It can
  always be regenerated.
- **Use the same switches every time.** Write the gnattest command down in the
  repository, in a script, a Makefile target, or AGENTS.md. Run it the same
  way every time. A different mode or `--tests-root` makes gnattest create
  fresh skeletons in a new place, and the harness then stops pointing at the
  tests you wrote.
- Don't modify the repository's `.gpr` files to set `package Gnattest`
  attributes. Use the command-line switches.

The server finds the tests through the harness's `Source_Dirs`, wherever they
are. It finds the harness in `<object dir>/gnattest/harness/` (or
`Gnattest'Harness_Dir`). If you use `--harness-dir` to put it elsewhere, set
`tests_project = "<that dir>"` in `.lal-mcp.toml`.

## What the MCP tools tell you

### `reload_project`

`reload_project` reports which harness is loaded, its test packages, and every
driver with its units, its runner, and whether that runner is built yet:

```
[project reloaded: requested; 31 sources, GNATtest harness app/obj/gnattest/harness/test_drivers.gpr (separate drivers: 3) (+0 / -0)]
GNATtest harness (separate drivers): app/obj/gnattest/harness/test_drivers.gpr loaded as root (it withs the project); 3 test package(s):
  app/obj/gnattest/tests/bit_ops-test_data-tests.adb
  app/obj/gnattest/tests/orphan-test_data-tests.adb
  hal/obj/gnattest/tests/hal-test_data-tests.adb
3 driver(s) (also listed in app/obj/gnattest/harness/test_drivers.list); build all with gprbuild -P ... run each runner:
  app/obj/gnattest/harness/Bit_Ops.Test_Data.Tests/test_driver.gpr: units bit_ops; runner .../bit_ops-test_data-tests-suite-test_runner.exe
  app/obj/gnattest/harness/Hal.Test_Data.Tests/test_driver.gpr: units hal; runner .../hal-test_data-tests-suite-test_runner.exe (not built)
```

You rarely need to call it. A harness generated, regenerated or deleted while
the server runs is picked up before the next call. So are added or removed
drivers and test files.

### `test_model`

`test_model <spec file> <declaration line>` reports what to test: parameters
with ranges and boundary values, contracts, branches, raises, global state,
callees and bit operations. With a harness loaded, it also shows where the test
goes and what runs it:

```
gnattest test: app/obj/gnattest/tests/bit_ops-test_data-tests.adb:36 Test_Set_Bit (generated skeleton, not implemented yet: write the test between the `--  end read only` and `--  begin read only` markers)
gnattest driver: app/obj/gnattest/harness/Bit_Ops.Test_Data.Tests/test_driver.gpr; runner app/obj/gnattest/harness/Bit_Ops.Test_Data.Tests/bit_ops-test_data-tests-suite-test_runner.exe
```

The driver is matched by the spec position each driver registers
(`"bit_ops.ads:14:4:"`), not by name. That makes it exact for overloads and for
`=test` drivers. When it says `implemented` instead of `generated skeleton`, a
test exists already: extend it rather than starting another.

If it says `gnattest test: none generated ...`, regenerate with the usual
command. Don't write a test by hand that gnattest would generate.

## Writing a test

Follow the `ada-unit-tests` skill. The parts that matter most:

1. **Check the link closure first.** Run `link_info <unit>`. A
   `missing body for X` or an unowned foreign symbol means that driver can't
   link. Fix that or report it before writing tests.
2. **Get the facts.** Run `test_model` for each overload, by its declaration
   line from `outline`.
3. **Use an independent oracle.** Derive every expected value from the
   documentation or contract that `test_model` prints. Compute it separately,
   for example by hand or with a small script. Never copy it from the body or
   from a previous run. Write down the width, the mask in hex, and the wrap or
   raise behavior for bit operations.
4. **Write only between the markers.** This is the generated skeleton with its
   default `Assert` replaced. The expected values assume the specified
   behavior is "returns `W` with bit `Bit` set".

   ```ada
   --  id:2.2/8d4e98c3a477d8a7/Set_Bit/1/0/
      procedure Test_Set_Bit (Gnattest_T : in out Test) is
      --  bit_ops.ads:14:4:Set_Bit
   --  end read only

         pragma Unreferenced (Gnattest_T);

      begin

         AUnit.Assertions.Assert (Set_Bit (16#0000#, 0) = 16#0001#, "bit 0 of 16#0000# -> 16#0001#");
         AUnit.Assertions.Assert (Set_Bit (16#0000#, 15) = 16#8000#, "top bit (15) of a 16-bit Word");
         AUnit.Assertions.Assert (Set_Bit (16#FFFF#, 15) = 16#FFFF#, "an already set bit stays set");

   --  begin read only
      end Test_Set_Bit;
   --  end read only
   ```

   Never edit the read-only regions or the `--  id:` and position markers.
   gnattest uses them to keep your code when it regenerates.
5. **Use one assertion per case,** each with a message naming the case. Put
   shared setup in `Test_Data` (`Set_Up` / `Tear_Down`).

## Building and running

```bash
# all drivers
gprbuild -P app/obj/gnattest/harness/test_drivers.gpr
# one unit's driver (when another unit doesn't compile, or to iterate fast)
gprbuild -P app/obj/gnattest/harness/Bit_Ops.Test_Data.Tests/test_driver.gpr
# run it
app/obj/gnattest/harness/Bit_Ops.Test_Data.Tests/bit_ops-test_data-tests-suite-test_runner
```

Each result is reported against the subprogram under test:

```
bit_ops.ads:17:4: error: corresponding test FAILED: Test not implemented. (bit_ops-test_data-tests.adb:65)
4 tests run: 0 passed; 4 failed; 0 crashed.
```

`Test not implemented.` is an unwritten skeleton, not a bug in the code.

Some things don't work as you might expect:

- **The aggregate build stops at the first unit that doesn't compile.** In the
  fixture, `Orphan` has no body (`cannot generate code for file orphan.ads`).
  Build the drivers you need from their own projects, and report the broken
  unit. `link_info` shows it beforehand as `missing body`.
- **`gnattest test_drivers.list` didn't run the built drivers** in our test with
  26.1: it reported `no test driver in source table`. Run each runner directly,
  from `reload_project` or `test_model`, or from the list file.

## Configuration

In the repository's `.lal-mcp.toml`:

```toml
project = "app/app.gpr"
# tests_project = "app/obj/gnattest/harness"   # only if the harness isn't in <obj>/gnattest/harness
# gnattest = false                              # don't load a harness at all
```

`tests_project` accepts the harness directory, `test_drivers.gpr` or
`test_driver.gpr`. The command-line equivalents are `--tests-project` and
`--no-gnattest`. Detection uses `gprinspect`, so GNAT Pro's `bin` must be on
the server's `PATH`.

## Troubleshooting

| you see | it means | do |
|---|---|---|
| `no GNATtest harness yet (expected at .../test_driver.gpr, or test_drivers.gpr ...)` | not generated yet, or in another directory | run gnattest, or set `tests_project` |
| `GNATtest harness ... not loaded (...); using the project only` | the harness is broken or stale | regenerate with the usual switches |
| `note: both test_drivers.gpr and test_driver.gpr exist; loaded the newer ...` | the mode was switched, and the old harness is left over | delete the leftover file, or regenerate in the intended mode |
| a single `test_driver.gpr` after `--separate-drivers` | spec files were passed on the command line | rerun without file arguments |
| `gnattest driver: none of the harness drivers registers this test` | the harness is older than the subprogram | regenerate |
| `(not built)` next to a runner | nothing to run yet | build that driver |
| tests reappear as fresh skeletons in a new place | different `--tests-root` / mode than before | regenerate with the original switches; move your bodies back |

## Best practices

- **Generate the same way every time.** Use one documented command, the same
  mode and the same `--tests-root`.
- **Put the tests in the source tree and commit them.** Leave the harness in
  `obj/` and don't commit it.
- **Use `--separate-drivers=unit`.** Use `=test` only when tests must be isolated.
- **Generate for the whole project, with no file arguments.** It's required for
  separate drivers, and it's harmless.
- **Check `link_info` before writing or building tests.** A missing body or an
  unowned symbol is a guaranteed build failure.
- **Use `test_model` for both what and where.** It gives the facts to test and
  the exact procedure and runner. Don't search for test files by name.
- **Expected values come from the specification.** Never take them from the
  implementation or from observed output.
- **Stay between the read-only markers.** Never edit the harness, and never edit
  the repository's `.gpr` files.
- **Repair once.** After a failure, make one targeted query, one fix and one
  rerun. If the same failure repeats, stop and report it.
- **Treat `[status: INCOMPLETE ...]`, `unresolved` and `[project warnings ...]`
  as blockers,** and say so out loud. Don't work around them.
