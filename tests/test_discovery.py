"""Unit tests for project discovery (no libadalang needed)."""

from __future__ import annotations

import os
import pathlib

import pytest

from lal_mcp.discovery import CliSettings, ConfigError, find_config, resolve, uri_to_path


def _proj(d: pathlib.Path, name: str = "app.gpr") -> pathlib.Path:
    (d / name).write_text("project App is end App;\n")
    return d / name


def test_explicit_project_wins(tmp_path):
    (tmp_path / ".lal-mcp.toml").write_text('project = "other.gpr"\n')
    r = resolve(CliSettings(project=str(_proj(tmp_path)), scenario={"A": "1"}), [], str(tmp_path))
    assert r.project.endswith("app.gpr") and r.source.endswith("from -P") and r.scenario == {"A": "1"}


def test_config_file_relative_paths_and_cli_override(tmp_path):
    (tmp_path / "build").mkdir()
    _proj(tmp_path / "build", "mission.gpr")
    (tmp_path / ".lal-mcp.toml").write_text(
        'project = "build/mission.gpr"\ntimeout = 5\n[scenario]\nBUILD = "debug"\nOS = "linux"\nFAST = true\n')
    sub = tmp_path / "src" / "deep"
    sub.mkdir(parents=True)
    r = resolve(CliSettings(scenario={"OS": "windows"}), [], str(sub))  # found walking up
    assert r.project == os.path.normpath(str(tmp_path / "build" / "mission.gpr"))
    assert r.workspace == os.path.abspath(str(tmp_path))  # default: the config file's directory
    assert r.scenario == {"BUILD": "debug", "OS": "windows", "FAST": "true"}  # -X wins
    assert r.timeout == 5 and r.max_chars == 8000
    assert "server working directory" in r.source


def test_roots_preferred_over_cwd(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    _proj(a, "a.gpr")
    _proj(b, "b.gpr")
    (a / ".lal-mcp.toml").write_text('project = "a.gpr"\n')
    (b / ".lal-mcp.toml").write_text('project = "b.gpr"\n')
    r = resolve(CliSettings(), [str(b)], str(a))
    assert r.project.endswith("b.gpr") and "client workspace root" in r.source


def test_single_gpr_fallback_and_ambiguity(tmp_path):
    _proj(tmp_path, "only.gpr")
    r = resolve(CliSettings(), [], str(tmp_path))
    assert r.project.endswith("only.gpr") and "the only .gpr" in r.source
    _proj(tmp_path, "second.gpr")
    with pytest.raises(ConfigError, match=r"several \.gpr files .*only\.gpr, second\.gpr"):
        resolve(CliSettings(), [], str(tmp_path))


def test_nothing_found(tmp_path):
    with pytest.raises(ConfigError, match="no .lal-mcp.toml found"):
        resolve(CliSettings(), [], str(tmp_path))


@pytest.mark.parametrize("body, msg", [
    ('projet = "x.gpr"\n', "unknown setting"),
    ("timeout = 5\n", "missing project"),
    ('project = "missing.gpr"\n', "project file not found"),
    ('project = "app.gpr"\nmax_chars = -1\n', "positive integer"),
    ('project = "app.gpr"\n[scenario]\nX = [1]\n', "scenario"),
    ("project = \n", "cannot read"),
])
def test_bad_config_files(tmp_path, body, msg):
    _proj(tmp_path)
    (tmp_path / ".lal-mcp.toml").write_text(body)
    with pytest.raises(ConfigError, match=msg):
        resolve(CliSettings(), [], str(tmp_path))


def test_explicit_config_flag(tmp_path):
    _proj(tmp_path)
    cfg = tmp_path / "custom.toml"
    cfg.write_text('project = "app.gpr"\n')
    r = resolve(CliSettings(config=str(cfg)), [], os.path.abspath(os.sep))
    assert r.project.endswith("app.gpr") and "--config" in r.source


def test_find_config_prefers_dotfile_and_nearest(tmp_path):
    (tmp_path / "lal-mcp.toml").write_text("")
    inner = tmp_path / "x"
    inner.mkdir()
    (inner / ".lal-mcp.toml").write_text("")
    assert find_config([str(inner)]) == str(inner / ".lal-mcp.toml")
    assert find_config([str(tmp_path)]) == str(tmp_path / "lal-mcp.toml")


def test_uri_to_path(tmp_path):
    assert uri_to_path(tmp_path.as_uri()) == os.path.abspath(str(tmp_path))
    assert uri_to_path("https://example.com/x") is None
