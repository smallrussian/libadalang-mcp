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
    from lal_mcp.tools import navigate, outline as outline_mod, units

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

