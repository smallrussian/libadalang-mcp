<!--
Copy the section below into the AGENTS.md of an Ada repository that has the
`ada` MCP server (lal-mcp) configured. It is written for the agent, not for people.
-->

## Ada code: use the `ada` MCP tools

This repository is Ada, built from its GPR project. The `ada` MCP server answers
questions with Libadalang's name resolution over that project. Its answers are
facts with `path:line` locations; your memory of similar code is not.

**Verify before asserting.** Before you state a subprogram's signature, a type's
components, which overload is called, or who calls something, check it with the
tools. Do the same before editing code that depends on any of those facts.
Quote declaration text exactly as the tools return it.

Use the tools instead of reading whole files:

- Which file holds unit `X`: `find_unit`. All files of a package (spec, body,
  `separate` bodies, children, dependencies): `package_files`.
- What a unit declares: `outline`. It gives signatures and record components
  with line numbers. Read a file only for the lines you need after that.
- What a name at `file:line:column` refers to: `goto_definition`. It returns the
  declaration text and where the body or stub is.
- Who uses an entity: `find_references`. It follows overloads exactly, unlike grep.
- Anything else: `run_lal` with a short Libadalang query. Check API names with
  `api_help` and node shapes with `dump` first; don't guess them.

Treat these as unknowns, and say so:

- `unresolved: ...` means name resolution failed or the unit isn't in the
  project. Don't fill the gap with a guess, and don't pick the nearest
  similarly named entity.
- `[approximate]` means the answer came from a fallback or from a file with parse
  errors. Mention it, and confirm another way before relying on it.
- `[project warnings ...]` at the top of a response means the project loaded
  with problems (e.g. no toolchain, so runtime units won't resolve). Report it.
- `no declaration: ...` means the name has nothing to go to (an attribute, a
  predefined operator, a pragma name). That's expected, not a failure.

Don't create, modify or generate `.gpr` files. The project file is fixed. If a
tool says a unit isn't in the project, report that instead of working around it.
