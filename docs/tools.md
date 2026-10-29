# lal-mcp tool reference

What each tool answers, what its output looks like, and how to read it.
Setup, configuration and client wiring are in the [README](../README.md);
the GNATtest side is in [gnattest.md](gnattest.md).

## Reading any answer

- **Locations** are `path:line`, relative to the workspace root, with forward
  slashes. Quote declaration text exactly as shown.
- **Unknowns are stated, never guessed.** Treat these as "don't know" and say
  so instead of filling the gap:
  - `unresolved: ...`: name resolution failed, or the unit isn't in the project.
  - `[approximate]`: the result came from an imprecise fallback, a file with
    parse errors, or a text search (C/C++ symbols).
  - `[status: INCOMPLETE: ...]` (`link_info`, `test_model`): the first line
    lists every fact that couldn't be established.
- **Size cap.** Each answer is at most 8,000 characters by default
  (`max_chars`). A cut answer says so and says how to narrow the query.
- **Errors are results.** A failing property or a bad argument comes back as a
  short message with `isError: true`, so the model can correct itself.
- **First call.** The first answer starts with `[project: ... from ...]`,
  naming the project that was loaded. After a project change, an answer starts
  with `[project reloaded: ...]`.

## Which tool

| question | tool |
| --- | --- |
| which file holds unit X? | `find_unit` |
| what files make up package X (body, `separate` subunits, children, dependencies)? | `package_files` |
| what does unit X declare? | `outline` |
| what does the name at file:line:column refer to? | `goto_definition` |
| who uses it? | `find_references` |
| what does X need to link (imported C symbols, owning projects)? | `link_info` |
| what do I need to know to test this subprogram? | `test_model` |
| anything else | `run_lal`, with `api_help` and `dump` for the API and tree shapes |
| the project changed and the server didn't notice | `reload_project` |

## Navigation

### `find_unit(name)`

The spec and body files of a unit, using the project's own naming scheme
(`Naming` package, non-GNAT file names).

### `package_files(name, children=true, deps=false, specs_only=false)`

The spec, the body, every `separate` subunit (found through the body stubs,
not by file name), and the child units. With `deps=true`, also the transitive
`with` closure. With `specs_only=true`, specs only.

### `outline(target)`

Declarations with line numbers and subprogram signatures, without bodies.
`target` is a unit name (outlines its spec) or a file path (any file, body
included). Use it instead of reading a file to see what's in it.

### `goto_definition(file, line, column)`

What the name at that position resolves to: its declaration's location and
text (capped), and its other parts (spec, body, stub).
Overloads are resolved by Libadalang, not by name.

### `find_references(file, line, column, limit=50)`

Every reference to the entity at that position, as `path:line: <source line>`.
On gnatcoll-core: 100% recall, 99.9% precision against the compiler's
cross-references ([metrics.md](metrics.md)).

## Build and test facts

### `link_info(name, deps=true)`

What a unit needs at link time. With `deps=true` (the default), this covers the
unit's whole dependency closure, including what its subunits `with`. Example
(from a mock repository):

```
[status: complete]
link info for N_MATH_ROUTINES_COMMON: 9 project files in 1 project(s), 3 runtime units
foreign imports/exports (1):
  Code/Ada_OFP/Shared/types/u_word_logicals.ads:23  SWAP_BYTES  import, convention C (pragma), symbol "uwl_swap_bytes"
      defined: Infra/Startup/hal/startup_hal.c:2 function definition, project StartupHal (Build/Projects/StartupHal.gpr) [approximate: C/C++ text search]
      defined: Build/_lib/startuphal/libstartuphal.a (project StartupHal) [nm]
projects (a client project needs a `with` for each non-root one):
  AppAdaOfp  Build/Projects/AppAdaOfp.gpr  library, library libappadaofp.a (built); languages ada; withed by root: aggregated by the root
      units: N_MATH_ROUTINES_COMMON, N_MATH_ROUTINES_COMMON.PLATFORM_TRANSFORM, U_BITRANGE16, ...
runtime units (GNAT runtime, linked automatically): Ada, Ada.Numerics, Ada.Numerics.Generic_Elementary_Functions
```

How to read it:

- **Foreign imports and exports:** every `pragma Import`/`Export`/`Interface`
  and `Import`/`Export` aspect, with its convention and link symbol (the
  explicit external name, or GNAT's default, the lower-cased Ada name).
  - `defined: <file>:<line> ... project P (<gpr>)` comes from a text search of
    the project tree's C/C++ files, hence `[approximate]`. With several
    definitions, they're probably `#ifdef` variants.
  - `defined: <library> [nm]` comes from the symbol table of a built library.
  - `unresolved: no definition of "sym" found`: nothing in the project tree
    defines it. Standard C library functions (`sin`, `memcpy`, ...) always
    show this way, because the linker adds them. It's a gap to check, not
    necessarily an error.
  - `compiler intrinsic: no link symbol` (convention `Intrinsic`): nothing to link.
- **Projects:** the GPR projects that own the closure. A test or client
  project needs a `with` for each one the root doesn't already bring in.
  Shows library files (built or not), languages and linker options.
- **Missing bodies:** subprograms declared in a spec with no body anywhere in
  the project (imported ones excluded).
- **Runtime units:** units the GNAT runtime provides.

Needs GNAT's `bin` on the server's `PATH` (it uses `gprinspect` and `nm`).

### `test_model(file, line, column=0)`

Facts for testing one subprogram. Give the spec file and the line of the
declaration; `column=0` means the subprogram declared on that line. Example:

```
[status: complete]
test model: N_MATH_ROUTINES_COMMON.LIMIT_SYMMETRIC [function]  parts: SubpDecl ...ads:6, SubpBody ...adb:7
signature: function LIMIT_SYMMETRIC (VALUE, LIMIT : REAL) return REAL
documentation (Code/Ada_OFP/Shared/math/n_math_routines_common.ads:6), the specification for expected values:
  | Returns VALUE limited to the range -LIMIT .. LIMIT.
contracts:
  Pre => LIMIT >= 0.0  (Code/Ada_OFP/Shared/math/n_math_routines_common.ads:7)
parameters:
  VALUE, LIMIT : in REAL
      type REAL (Code/Ada_OFP/Shared/types/u_number_types.ads:7): floating point
result: REAL
gnattest test: Tests/Ada/Unit/N_MATH_ROUTINES_COMMON/n_math_routines_common-test_data-tests.adb:36 Test_LIMIT_SYMMETRIC (generated skeleton, not implemented yet: ...)
gnattest driver: .../test_driver.gpr; runner .../...-test_runner.exe (not built yet)
branches (2):
  Code/Ada_OFP/Shared/math/n_math_routines_common.adb:9  if VALUE > LIMIT
  Code/Ada_OFP/Shared/math/n_math_routines_common.adb:11  elsif VALUE < -LIMIT
raises/handlers: none explicit (implicit Constraint_Error from range checks still applies)
returns (3): ...
global state: none (pure with respect to package state)
calls (test-double candidates): none
expected results: derive from the specification/contract and compute them independently; ...
```

The sections:

- **documentation:** the comments attached to the declaration, if any. When
  there are none, the behavior isn't documented and tests can only
  characterize what the code does today.
- **body:** normally just its location. It says so instead when there is no
  body to read: an imported (foreign) subprogram (test it through a double,
  see `link_info`), a generic instance, a generic formal, an abstract one, or
  `MISSING`.
- **contracts:** the subprogram's `Pre`, `Post`, `Contract_Cases`, `Global`
  and similar aspects.
- **parameters / result:** mode, type, and the type's range, with boundary
  values for scalar types (for modular types, the width too).
- **gnattest test / driver:** the generated test routine for this
  subprogram, whether it's still a skeleton, and the driver and runner that
  run it. Present only when a GNATtest harness is loaded (see the README's
  GNATtest section and `tests_project`).
- **branches, raises/handlers, returns:** each with its body line, so a test
  can aim at every path.
- **global state:** package-level variables the subprogram reads or writes.
- **calls:** callees that are candidates for test doubles (foreign imports,
  dispatching calls, other units).
- **bit/width operations:** shifts, rotates, and `and`/`or`/`xor` on modular
  types, where width and sign matter for expected values.
- **unresolved names:** names Libadalang couldn't resolve in the body; facts
  that depend on them are unknown.

It deliberately gives no expected values: those must come from the
documentation, a requirement, or an independent computation.

### `reload_project()`

Rebuilds the project view now and reports what's loaded (including the
GNATtest harness, its test packages and, with separate drivers, each driver
and whether its runner is built). Normally not needed: added or removed
files and `.gpr` edits are picked up before each call.

## Open-ended

### `run_lal(code)`

Runs a Python snippet against the loaded project and returns what it prints
(and the value of a final expression). Preloaded: `lal`, `ctx`, `prj`,
`unit(name_or_path)`, `units()`, `rel(node)` (`"path:line"`). Time-limited
(default 20 s). Use it for questions no other tool answers, e.g. all callers
of a subprogram with `p_find_all_calls`. The server's `instructions` include
a few ready examples.

`f_xxx` fields are syntax children; `p_xxx` properties are semantic and may
raise `lal.PropertyError` on code Libadalang can't resolve.

### `api_help(name)`

The signature and documentation of a Libadalang class or member from the
installed version, e.g. `WithClause` or `Name.p_referenced_decl`. Check here
instead of guessing a property name.

### `dump(target, start_line, end_line, max_depth=4)`

The syntax tree (node kinds and fields) of the smallest nodes covering that
line range, depth-limited. Use it to learn a node's shape before writing a
`run_lal` query. Full trees are never returned.
