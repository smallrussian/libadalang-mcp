<!--
Copy the section below into the AGENTS.md of an Ada repository that has the
`ada` MCP server (lal-mcp) configured. It applies to all work in the repo:
reading, changing, and testing code. Test-writing and test-running procedures
live in the skills under docs/agents/skills/ (copy to .agents/skills/).
It is written for the agent, not for people.
-->

## Ada code: use the `ada` MCP tools

This repository is Ada, built from its GPR project. The `ada` MCP server answers
questions with Libadalang's name resolution over that project, plus GPR and
linker metadata. Its answers are facts with `path:line` locations; your memory of
similar code is not.

**Verify before asserting.** Before you state a subprogram's signature, a type's
components or range, which overload is called, who calls something, or what a
unit needs to link, check it with the tools. Do the same before editing code
that depends on any of those facts. Quote declaration text exactly as the tools
return it.

Use the tools instead of reading whole files:

- Which file holds unit `X`: `find_unit`. All files of a package (spec, body,
  `separate` bodies, children, dependencies): `package_files`.
- What a unit declares: `outline`. It gives signatures and record components
  with line numbers. Read a file only for the lines you need after that.
- What a name at `file:line:column` refers to: `goto_definition`. Who uses an
  entity: `find_references`. It follows overloads exactly, unlike grep.
- What a unit needs to build and link: `link_info`. It reports:
  - every `pragma Import`/`Export` (or aspect) symbol, and the C/C++ source or
    library that defines it;
  - the GPR projects that own its dependencies (the `with` clauses a project
    using it needs);
  - linker options and missing bodies.
- Facts for testing or changing a subprogram: `test_model` (file and line of its
  declaration, from `outline`). It reports parameter types with ranges and
  boundary values, contracts, branches, raises, returns, global state, callees,
  and bit operations.
- Anything else: `run_lal`, with a short Libadalang query. Check API names with
  `api_help` and node shapes with `dump` first; don't guess them.

### Uncertainty gate: stop, don't guess

Treat each of these as a blocker for any step that depends on it:

- `[status: INCOMPLETE: ...]` as the first line of a `link_info` or `test_model`
  answer: an unresolved symbol, a missing body, unresolved names, parse errors,
  or a dependency no project owns.
- `unresolved: ...` from any tool: name resolution failed, or the unit isn't in
  the project.
- `[project warnings ...]` at the top of a response: the project loaded with
  problems. For example, no toolchain, so runtime units won't resolve.
- `[approximate]`: from a fallback or a file with parse errors. Confirm it
  another way before relying on it.

When one of these blocks a step, stop that step and report exactly what's
missing (the tool's own text), and what would resolve it. Do not:

- fall back to reading whole files to reconstruct what the tool couldn't;
- substitute a similarly named entity, a guessed symbol owner, or a guessed `with`;
- continue with invented values "to be fixed later".

These are not blockers:

- `no declaration: ...`: the name has nothing to go to (an attribute, a
  predefined operator, a pragma name).
- An error in **your own query**:
  - a mistyped Libadalang API name in `api_help` or `run_lal` (the answer lists
    close matches);
  - a `file:line:column` that isn't on a name (the answer lists the names on
    that line);
  - a `run_lal` snippet error.

  Correct the query and retry once. The gate is about facts of the code that
  can't be established, not about your request's mistakes.

If a tool answer looks wrong (for example, a type described implausibly), say
so and confirm with a second tool (`goto_definition`, `outline`). Don't build on it.

### Build and link changes

Before adding a `with` clause, a dependency on another unit, or code that calls
an imported (foreign) subprogram, run `link_info` on the unit. Every symbol it
lists must have an owner (C/C++ source, project library, or GNAT runtime), and
every owning project must be withed by the project you build. An owner shown as
`[approximate: C/C++ text search]` comes from a text match. When it lists
several definitions (platform `#if` variants), state which one your build
compiles.

Never modify the repository's existing `.gpr` files. New project files are
allowed only for test harnesses, under the test area, in the test runner
workflow. Their `with` clauses come from `link_info`, not from guesses.

### Failures

After a build, link, or test failure:

1. Run one targeted query on the specific failure: `link_info` for an undefined
   symbol, `goto_definition` / `test_model` for a type or value error.
2. Make one focused repair.
3. Rerun.

If the same failure comes back, stop and report it, with the tool output you
based the repair on. No repeated repair loops.
