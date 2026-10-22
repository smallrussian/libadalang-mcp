"""Tool registration. Each tool body lives in a module here and returns (text, is_error)."""

from __future__ import annotations

from typing import Any, Callable

from mcp.server.mcpserver import MCPServer


def register(app: MCPServer, call: Callable[..., Any]) -> None:
    """Register every tool on `app`. `call(fn, *args)` runs fn(session, *args) safely."""
