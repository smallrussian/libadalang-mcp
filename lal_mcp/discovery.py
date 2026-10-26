"""Find which GPR project to load, so one global MCP entry works in every repo.

Order (first match wins):
  1. `-P/--project` on the command line (explicit, as before).
  2. `--config FILE` on the command line.
  3. `.lal-mcp.toml` (or `lal-mcp.toml`) found by walking up from the client's
     workspace: the MCP roots the client reports, or else the server's cwd.
  4. No config file: if the workspace root holds exactly one `.gpr`, use it.
     Several or none: an error that lists what was found and how to configure.

Command-line `-w/-X/--target/--RTS/--max-chars/--timeout` override the file.

Config file (paths relative to the file's directory):

    project = "mission.gpr"        # required
    workspace = "."                # default: the file's directory
    target = "x86_64-linux"        # optional
    runtime = "light"              # optional
    max_chars = 8000               # optional
    timeout = 20                   # optional, run_lal seconds
    [scenario]                     # optional, -X variables
    BUILD = "debug"
"""

from __future__ import annotations

import glob
import os
import tomllib
from dataclasses import dataclass, field
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

CONFIG_NAMES = (".lal-mcp.toml", "lal-mcp.toml")
KNOWN_KEYS = {"project", "workspace", "target", "runtime", "max_chars", "timeout", "scenario",
              "tests_project", "gnattest"}
DEFAULT_MAX_CHARS = 8000
DEFAULT_TIMEOUT = 20


class ConfigError(Exception):
    """Project could not be determined; the message tells the user what to do."""


@dataclass
class CliSettings:
    """What came from the command line; None means 'not given'."""

    project: str | None = None
    config: str | None = None
    workspace: str | None = None
    scenario: dict[str, str] = field(default_factory=dict)
    target: str | None = None
    runtime: str | None = None
    max_chars: int | None = None
    timeout: int | None = None
    tests_project: str | None = None  # explicit GNATtest harness (test_driver.gpr)
    gnattest: bool | None = None      # auto-detect the GNATtest harness (default: on)


@dataclass
class Resolved:
    project: str
    workspace: str
    scenario: dict[str, str]
    target: str | None
    runtime: str | None
    max_chars: int
    timeout: int
    source: str  # how the project was found, shown to the user once
    tests_project: str | None = None
    gnattest: bool = True


def uri_to_path(uri: str) -> str | None:
    """file:// URI (an MCP root) to a local path; None for other schemes."""
    u = urlparse(uri)
    if u.scheme != "file":
        return None
    path = url2pathname(unquote(u.path))
    if os.name == "nt" and u.netloc and u.netloc not in ("", "localhost"):
        path = f"\\\\{u.netloc}{path}"  # UNC share
    return os.path.abspath(path)


def find_config(start_dirs: list[str]) -> str | None:
    """Nearest config file walking up from each start dir, in order."""
    for start in start_dirs:
        d = os.path.abspath(start)
        while True:
            for name in CONFIG_NAMES:
                p = os.path.join(d, name)
                if os.path.isfile(p):
                    return p
            parent = os.path.dirname(d)
            if parent == d:
                break
            d = parent
    return None


def load_config(path: str) -> dict:
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    unknown = set(data) - KNOWN_KEYS
    if unknown:
        raise ConfigError(f"{path}: unknown setting(s) {', '.join(sorted(unknown))}; "
                          f"known: {', '.join(sorted(KNOWN_KEYS))}")
    if not isinstance(data.get("project"), str) or not data["project"].strip():
        raise ConfigError(f'{path}: missing project = "<file>.gpr"')
    scen = data.get("scenario", {})
    if not isinstance(scen, dict) or not all(isinstance(v, (str, int, bool)) for v in scen.values()):
        raise ConfigError(f"{path}: [scenario] must map variable names to strings")
    for key in ("max_chars", "timeout"):
        if key in data and not (isinstance(data[key], int) and data[key] > 0):
            raise ConfigError(f"{path}: {key} must be a positive integer")
    if "gnattest" in data and not isinstance(data["gnattest"], bool):
        raise ConfigError(f"{path}: gnattest must be true or false")
    if "tests_project" in data and not (isinstance(data["tests_project"], str) and data["tests_project"].strip()):
        raise ConfigError(f'{path}: tests_project must be a path, e.g. "obj/gnattest/harness/test_driver.gpr"')
    return data


def _from_file(path: str, cli: CliSettings, how: str) -> Resolved:
    data = load_config(path)
    base = os.path.dirname(os.path.abspath(path))
    project = os.path.normpath(os.path.join(base, data["project"]))
    if not os.path.isfile(project):
        raise ConfigError(f"{path}: project file not found: {project}")
    workspace = cli.workspace or os.path.normpath(os.path.join(base, data.get("workspace", ".")))
    scenario = {k: str(v).lower() if isinstance(v, bool) else str(v) for k, v in data.get("scenario", {}).items()}
    scenario.update(cli.scenario)  # command line wins
    return Resolved(
        project=project, workspace=os.path.abspath(workspace), scenario=scenario,
        target=cli.target or data.get("target"), runtime=cli.runtime or data.get("runtime"),
        max_chars=cli.max_chars or data.get("max_chars", DEFAULT_MAX_CHARS),
        timeout=cli.timeout if cli.timeout is not None else data.get("timeout", DEFAULT_TIMEOUT),
        source=f"{os.path.basename(project)} from {path} ({how})",
        tests_project=(os.path.abspath(cli.tests_project) if cli.tests_project else
                       os.path.normpath(os.path.join(base, data["tests_project"])) if data.get("tests_project")
                       else None),
        gnattest=cli.gnattest if cli.gnattest is not None else data.get("gnattest", True),
    )


def resolve(cli: CliSettings, roots: list[str], cwd: str) -> Resolved:
    """Decide the project. `roots` are local paths of the client's MCP roots (may be empty)."""
    if cli.project:
        project = os.path.abspath(cli.project)
        return Resolved(
            project=project, workspace=os.path.abspath(cli.workspace or os.path.dirname(project)),
            scenario=dict(cli.scenario), target=cli.target, runtime=cli.runtime,
            max_chars=cli.max_chars or DEFAULT_MAX_CHARS, timeout=cli.timeout if cli.timeout is not None else DEFAULT_TIMEOUT,
            source=f"{os.path.basename(project)} from -P",
            tests_project=os.path.abspath(cli.tests_project) if cli.tests_project else None,
            gnattest=cli.gnattest if cli.gnattest is not None else True,
        )
    if cli.config:
        if not os.path.isfile(cli.config):
            raise ConfigError(f"--config file not found: {cli.config}")
        return _from_file(cli.config, cli, "--config")

    starts = [r for r in roots if os.path.isdir(r)] or [cwd]
    where = "client workspace root" if roots else "server working directory"
    found = find_config(starts)
    if found:
        return _from_file(found, cli, f"found from the {where} {starts[0]}")

    root = starts[0]
    gprs = sorted(glob.glob(os.path.join(root, "*.gpr")))
    if len(gprs) == 1:
        project = os.path.abspath(gprs[0])
        return Resolved(
            project=project, workspace=os.path.abspath(cli.workspace or root), scenario=dict(cli.scenario),
            target=cli.target, runtime=cli.runtime, max_chars=cli.max_chars or DEFAULT_MAX_CHARS,
            timeout=cli.timeout if cli.timeout is not None else DEFAULT_TIMEOUT,
            source=(f"{os.path.basename(project)}: the only .gpr in the {where} {root}, no .lal-mcp.toml; "
                    "scenario variables are GPR defaults"),
            tests_project=os.path.abspath(cli.tests_project) if cli.tests_project else None,
            gnattest=cli.gnattest if cli.gnattest is not None else True,
        )
    hint = ('Add a .lal-mcp.toml at the repository root, e.g.\n'
            '  project = "path/to/root.gpr"\n  [scenario]\n  BUILD = "debug"\n'
            'or pass -P/--project to the server.')
    if gprs:
        listed = ", ".join(os.path.basename(g) for g in gprs[:10])
        raise ConfigError(f"several .gpr files in {root} ({listed}) and no .lal-mcp.toml to choose one. {hint}")
    raise ConfigError(f"no .lal-mcp.toml found in {', '.join(starts)} or its parents, and no .gpr in {root} "
                      f"(searched from the {where}). {hint}")
