---
name: ada-unit-tests
description: Write or extend unit tests for Ada subprograms using the ada MCP tools (lal-mcp) - semantic test model, independently derived expected values, link closure checked first. Use when asked to create, add, fix or extend Ada tests. Do not use when only asked to build, run or validate existing tests (use ada-test-runner).
---

# Writing Ada unit tests

Follow these steps in order. Each step names the tool that grounds it. When a
step hits the uncertainty gate (AGENTS.md: `[status: INCOMPLETE ...]`,
`unresolved` facts about the code, `[project warnings ...]`), stop and report.
Don't work around it. A mistake in your own query (a wrong API name in
`api_help`/`run_lal`, a position not on a name) isn't a gate: fix it and retry once.

The specification for expected values is the declaration's documentation
(`test_model` prints it), its contracts, and the project's requirements. It's
never the body. If the specification leaves a case open, list it as an open
question rather than guessing.

## 1. Scope

- `outline` on the unit under test: list the public operations, including every
  overload, with their declaration lines.
- Pick the operations to cover. Overloads are separate operations: identify each
  by its declaration `file:line`, never by name alone.

## 2. Build/link closure, before writing anything

- `link_info <unit>`. Requirements:
  - the first line is `[status: complete]`;
  - every imported symbol has an owner;
  - every owning project is withed by the project you'll build;
  - no missing bodies.
- Record what the test harness needs: the projects to `with`, the linker options,
  and the foreign symbols. A foreign symbol needs either the real library linked
  in or a test double that defines the same symbol.
- If it's incomplete, stop: report the missing symbol, body or project.

## 3. Semantic test model, one per operation

`test_model <file> <line>` (the declaration line from `outline`). From its
output, write a compact matrix. Put it in the test file as a comment, or in your
answer:

| item | source |
|---|---|
| operation and overload (`file:line`) | test_model header |
| inputs: type, range, boundary values | `parameters` (modular: width, modulus) |
| preconditions and contracts | `contracts` |
| branch/path conditions, and which input reaches each | `branches` |
| exceptions: explicit raises, handlers, implicit range checks | `raises/handlers` |
| state read/written before and after | `global state` |
| expected result, and how it was derived | step 4, never from the model |
| test doubles needed | `calls` (foreign imports, dispatching, other units) |

Cover each branch at least once. Cover each boundary value of each input. Cover
each explicit raise, and the `Pre` boundary (just inside, and just outside if
the precondition is checked).

## 4. Oracle: expected values

For every expected value:

1. **Derive** it from the specification: the contract (`Pre`/`Post`), the
   documented behavior, or the requirement. Write down the derivation.
2. **Compute** it independently with a reference model. Do the arithmetic
   yourself, or with a small script that doesn't call the code under test.
3. **Check representability:** the value fits the result type's range and kind
   (`test_model` gives both). Modular results wrap; signed results that don't fit
   raise `Constraint_Error`.
4. **Reject** any value copied from the code under test's output, a previous test
   run, a debugger session, or the implementation's own expressions. If the
   specification doesn't determine the value, say so and ask. Don't invent one.

Bit operations need extra care. State each of these explicitly in the derivation:

- the operand **width** (bits) and **modulus**;
- the **mask**, written in hex with its width;
- **signed vs unsigned** interpretation of the same bits, and any conversion
  between them (`test_model` lists the width conversions);
- **shift/rotate direction**, and what happens when the amount is ≥ the width;
- whether overflow **wraps** (modular), **raises** (signed), or is undefined (C code behind an import).

## 5. Write the tests

- Use the project's existing test framework and layout. Look for them with
  `package_files` / `find_unit`, not by guessing names.
- One test per matrix row, named after the operation, overload and case.
- Doubles: for foreign imports, define the same external symbol in the test
  harness, or link the real library. Choose based on step 2. For dispatching
  calls, use a test-local derived type.
- Never change the code under test to make a test pass.

## 6. Run and react

- Run the tests (see the `ada-test-runner` skill for building and running).
- Read the project's result file (e.g. `agent-result.json`).
- On a failure: one targeted MCP query on that failure, one focused repair, one
  rerun.
  - **A wrong expected value:** re-derive it (step 4). Never copy the observed value.
  - **A link error:** use `link_info`.
- If the same failure repeats, stop and report it. Include:
  - the failure;
  - the tool output you relied on;
  - your derivation.
