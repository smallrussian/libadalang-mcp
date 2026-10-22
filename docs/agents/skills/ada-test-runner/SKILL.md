---
name: ada-test-runner
description: Prepare, build and run existing Ada tests and report results, using the ada MCP tools (lal-mcp) to resolve build/link metadata. Runner-only - never writes or edits authored tests. Use when asked to run, build, validate or check the test runner/harness. Do not use to write or fix tests (use ada-unit-tests).
---

# Running Ada tests (runner-only)

This skill only **prepares and runs**. It never creates, edits or deletes
authored test code or expected values, even when tests fail. If a test needs
changing, report it, and leave it to the `ada-unit-tests` workflow on request.

## 1. Resolve metadata

- For each unit under test: run `link_info <unit>`. Collect:
  - the owning projects (the `with` clauses);
  - the linker options;
  - the foreign symbols and their owners.
- Gate: if any `link_info` first line is `[status: INCOMPLETE ...]`, or a tool
  says `unresolved` or shows `[project warnings ...]`, stop. Report exactly what's
  missing. Don't build with guessed `with` clauses or symbols.

## 2. Create or register the harness project

- Only a test-harness `.gpr`, under the test area. Never modify the repository's
  existing project files.
- Its `with` clauses are exactly the owning projects from step 1 that the
  harness doesn't already reach. Linker options come from those projects (they
  carry their own `Linker_Options`). Add others only if `link_info` shows a
  symbol owned outside any project.
- If the harness already exists, check it against step 1. Report differences;
  don't silently rewrite it.

## 3. Run

- Build, then run the project's test script.
- Collect the result file (e.g. `agent-result.json`).

## 4. Report

- Report passed and failed tests per test, with the failure text from the result
  file.
- **Build/link failures:** one targeted `link_info` query on the failing symbol
  or unit, one fix to the harness (never to tests or the code under test), one
  rerun. If the same failure repeats, stop and report it.
- **Test failures:** report them, and stop. Runner-only mode doesn't repair tests.
