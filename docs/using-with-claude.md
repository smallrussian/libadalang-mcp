# Using lal-mcp with Claude

The server is a standard stdio MCP server, so Claude Code and Claude Desktop use
it like Codex does. The requirements are the same as in the README: a Python
that can `import libadalang`, and `PATH` containing GNAT's `bin` directory.

Verified with Claude Code 2.1.259 on gnatcoll-core. Claude picked
`outline` → `find_references` on its own and returned the 7 call sites of
`GNATCOLL.JSON.Create_Object` that the GNAT compiler reports, without reading
any file.

## Claude Code

One command, per project (the `local` scope is the default):

```bash
claude mcp add ada -e PATH=/opt/gnatpro/26.1/bin:/usr/bin:/bin -- \
    /path/to/python /path/to/lal-mcp/lal_mcp/server.py -P /ws/mission.gpr -w /ws -XBUILD=debug
```

Windows:

```powershell
claude mcp add ada -e "PATH=C:\GNATPRO\26.1\bin;C:\Windows\system32;C:\Windows" -- `
    C:\path\to\python.exe C:\path\to\lal-mcp\lal_mcp\server.py -P C:\ws\mission.gpr -w C:\ws
```

To share it with the team, commit a `.mcp.json` at the root of the Ada
repository (`claude mcp add --scope project ...` writes the same file).
`${VAR}` is expanded from the user's environment, so each person sets
their own paths:

```json
{
  "mcpServers": {
    "ada": {
      "type": "stdio",
      "command": "${LAL_MCP_PYTHON}",
      "args": ["${LAL_MCP_HOME}/lal_mcp/server.py", "-P", "mission.gpr", "-w", ".", "-XBUILD=debug"],
      "env": { "PATH": "${GNATPRO_PREFIX}/bin:/usr/bin:/bin" }
    }
  }
}
```

Then check `claude mcp list`, or `/mcp` inside a session. The server should
show as connected. It answers `initialize` at once and loads the project on
the first tool call.

Guidance for Claude: copy the section in [AGENTS-snippet.md](AGENTS-snippet.md)
into the Ada repository's `CLAUDE.md`, or import it from there with
`@path/to/AGENTS-snippet.md`. In Claude Code the tools are named
`mcp__ada__outline`, `mcp__ada__goto_definition`, and so on. The snippet's plain
names (`outline`, ...) work as they are.

Allow the read-only tools without prompting, in `.claude/settings.json`:

```json
{ "permissions": { "allow": ["mcp__ada__*"] } }
```

`run_lal` executes Python in the server process. It's meant for read-only
queries, but if you want a prompt for it, allow the other tools individually
instead of `mcp__ada__*`.

## Claude Desktop

Edit `claude_desktop_config.json`: on Windows it's `%APPDATA%\Claude\claude_desktop_config.json`,
on macOS `~/Library/Application Support/Claude/claude_desktop_config.json`.
Then restart the app:

```json
{
  "mcpServers": {
    "ada": {
      "command": "C:\\path\\to\\python.exe",
      "args": ["C:\\path\\to\\lal-mcp\\lal_mcp\\server.py", "-P", "C:\\ws\\mission.gpr", "-w", "C:\\ws"],
      "env": { "PATH": "C:\\GNATPRO\\26.1\\bin;C:\\Windows\\system32;C:\\Windows" }
    }
  }
}
```

## Headless / scripted use

```bash
claude -p "Who calls Pkg.Op? Answer with file:line." \
    --mcp-config ada-mcp.json --strict-mcp-config --allowedTools "mcp__ada__*"
```

`scripts/eval_agent.py` uses this form to compare answers with and without the
tools; see [metrics.md](metrics.md).
