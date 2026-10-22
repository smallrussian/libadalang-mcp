"""Tool registration. Each tool body lives in a module here and returns (text, is_error)."""

from __future__ import annotations

from typing import Annotated, Callable

from mcp.server.mcpserver import MCPServer
from mcp_types import CallToolResult
from pydantic import Field

UnitName = Annotated[str, Field(description="Ada unit name, e.g. Vehicle.Engine (case-insensitive)")]
FilePath = Annotated[str, Field(description="Source file path relative to the workspace root (or a bare file name)")]
Line = Annotated[int, Field(description="1-based line number")]
Column = Annotated[int, Field(description="1-based column of a name on that line")]


def register(app: MCPServer, call: Callable[..., CallToolResult]) -> None:
    """Register every tool on `app`. `call(fn, *args)` runs fn(session, *args) and caps the result."""
    from lal_mcp.tools import navigate, openended, outline as outline_mod, units

    # Tools are async so they run on the event loop (main) thread: Libadalang
    # is not thread-safe and the run_lal timeout needs the main thread.

    @app.tool()
    async def find_unit(name: UnitName) -> CallToolResult:
        """Find the spec and body files of an Ada unit by name. Uses the project's GPR naming scheme."""
        return call(units.find_unit, name)

    @app.tool()
    async def package_files(
        name: UnitName,
        children: Annotated[bool, Field(description="Also list child units (Name.*)")] = True,
        deps: Annotated[bool, Field(description="Also list the transitive dependency closure")] = False,
        specs_only: Annotated[bool, Field(description="List spec files only")] = False,
    ) -> CallToolResult:
        """List the files that make up a unit: spec, body, separate subunits, child units. Optionally its dependency closure."""
        return call(units.package_files, name, children, deps, specs_only, hint="set children=false or specs_only=true")

    @app.tool()
    async def outline(
        target: Annotated[str, Field(description="Unit name (outlines its spec) or a file path")],
    ) -> CallToolResult:
        """Declarations of a unit or file with line numbers and subprogram signatures, without bodies. Use instead of reading the file."""
        return call(outline_mod.outline, target, hint="outline a smaller unit or a child package")

    @app.tool()
    async def goto_definition(file: FilePath, line: Line, column: Column) -> CallToolResult:
        """Resolve the name at file:line:column to its declaration, with the declaration text and its other parts (body, stub)."""
        return call(navigate.goto_definition, file, line, column)

    @app.tool()
    async def find_references(
        file: FilePath,
        line: Line,
        column: Column,
        limit: Annotated[int, Field(description="Max references to list")] = 50,
    ) -> CallToolResult:
        """List references to the entity named at file:line:column across the project, as path:line: source line."""
        return call(navigate.find_references, file, line, column, limit, hint="lower the limit")

    @app.tool()
    async def run_lal(
        code: Annotated[str, Field(description="Python snippet; print() what you need. Globals: lal, ctx, prj, unit(name_or_path), units(), rel(node)")],
    ) -> CallToolResult:
        """Run a Python snippet against the warm Libadalang context and return its printed output. Use for questions the other tools do not answer."""
        return call(openended.run_lal, code, hint="print less, or filter inside the snippet")

    @app.tool()
    async def api_help(
        name: Annotated[str, Field(description="Libadalang class or member, e.g. WithClause or Name.p_referenced_decl")],
    ) -> CallToolResult:
        """Show the signature and documentation of a Libadalang class, field or property. Use before guessing an API name in run_lal."""
        return call(openended.api_help, name, needs_project=False)

    @app.tool()
    async def dump(
        target: Annotated[str, Field(description="Unit name (its spec) or file path")],
        start_line: Annotated[int, Field(description="First line of the range (1-based)")],
        end_line: Annotated[int, Field(description="Last line of the range (1-based)")],
        max_depth: Annotated[int, Field(description="Max tree depth to expand")] = 4,
    ) -> CallToolResult:
        """Show the syntax tree (node kinds, f_ fields, line:col ranges) of the smallest nodes covering a line range. Use to learn node types before writing run_lal code."""
        return call(openended.dump, target, start_line, end_line, max_depth, hint="use a smaller line range or max_depth")
