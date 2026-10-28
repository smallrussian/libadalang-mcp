"""GNATtest (GNATdas) harness layouts: which harness project to load and, for
--separate-drivers harnesses, which driver (and runner executable) runs a given test.

Layouts in <object dir>/gnattest/harness (or Gnattest'Harness_Dir):
- single driver: test_driver.gpr (one runner, test_runner).
- separate drivers (gnattest --separate-drivers[=unit|test]): test_drivers.gpr, an
  aggregate project listing one driver project per unit (<Unit>.Test_Data.Tests/test_driver.gpr)
  or per test (<Unit>.Test_Data.Tests/driver_test_<name>_<hash>.gpr), plus test_drivers.list
  with the absolute path of every runner executable.
Everything here is read from the files gnattest wrote; nothing is generated.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

SINGLE = "test_driver.gpr"
SEPARATE = "test_drivers.gpr"
HARNESS_FILES = (SEPARATE, SINGLE)

_COMMENT = re.compile(r"--[^\n]*")
_PROJECT_FILES = re.compile(r"\bfor\s+Project_Files\s+use\s*\(([^)]*)\)", re.I)
_MAIN = re.compile(r"\bfor\s+Main\s+use\s*\(\s*\"([^\"]+)\"", re.I)
_EXEC_DIR = re.compile(r"\bfor\s+Exec_Dir\s+use\s*\"([^\"]+)\"", re.I)


def _read(path: str) -> str:
    try:
        with open(path, encoding="latin-1") as f:
            return _COMMENT.sub("", f.read())
    except OSError:
        return ""


def _gpr_path(base_dir: str, name: str) -> str:
    """A path written in a .gpr (gnattest writes `\\` on Windows), resolved against base_dir."""
    name = name.replace("\\", "/")
    return os.path.normpath(os.path.join(base_dir, name))


def candidates(harness_dir: str) -> list[str]:
    return [os.path.normpath(os.path.join(harness_dir, f)) for f in HARNESS_FILES]


def pick(harness_dir: str) -> tuple[str, str | None]:
    """(harness project to load, note). When neither exists, the single-driver path (the one
    to expect) is returned. When both exist (the mode was switched), the newer one wins."""
    sep, single = candidates(harness_dir)
    have = [p for p in (sep, single) if os.path.isfile(p)]
    if len(have) == 2:
        newer = max(have, key=os.path.getmtime)
        older = single if newer == sep else sep
        return newer, (f"both {os.path.basename(sep)} and {os.path.basename(single)} exist; loaded the newer "
                       f"{os.path.basename(newer)} ({os.path.basename(older)} is a leftover of the other mode)")
    if have:
        return have[0], None
    return single, None


def resolve_explicit(path: str) -> tuple[str, str | None]:
    """tests_project may name a harness file or the harness directory."""
    path = os.path.abspath(path)
    if os.path.isdir(path):
        return pick(path)
    return path, None


def is_aggregate(path: str) -> bool:
    return bool(re.search(r"\baggregate\s+project\b", _read(path), re.I))


def project_files(aggregate: str) -> list[str]:
    """The projects an aggregate lists in Project_Files, as absolute paths."""
    base = os.path.dirname(os.path.abspath(aggregate))
    out = []
    for m in _PROJECT_FILES.finditer(_read(aggregate)):
        out += [_gpr_path(base, n) for n in re.findall(r'"([^"]+)"', m.group(1))]
    return out


@dataclass
class Driver:
    project: str            # absolute path of the driver's .gpr
    units: list[str]        # units.list of its directory (lower case, as gnattest writes them)
    main: str | None        # absolute path of the driver's main (test runner) source
    runner: str | None      # absolute path of the runner executable
    files: list[str] = field(default_factory=list)  # sources naming the tests this driver runs

    @property
    def directory(self) -> str:
        return os.path.dirname(self.project)

    def runs(self, spec_file: str, line: int, col: int) -> bool:
        """Does this driver register the test for the subprogram declared at spec_file:line:col?
        gnattest names each test case by that position, e.g. "bit_ops.ads:14:4:"."""
        needle = f'"{os.path.basename(spec_file)}:{line}:{col}:'.lower()
        return any(needle in _read(f).lower() for f in self.files)


def drivers(aggregate: str, _seen: set[str] | None = None) -> list[Driver]:
    """The drivers of a separate-drivers harness, with their units and runner executables.
    An aggregate that lists other aggregates (e.g. one test_drivers.gpr per package) is
    followed down to the driver projects."""
    seen = _seen if _seen is not None else set()
    key = os.path.normcase(os.path.abspath(aggregate))
    if key in seen:
        return []
    seen.add(key)
    listed = {}
    for ln in _read(os.path.join(os.path.dirname(aggregate), "test_drivers.list")).splitlines():
        ln = ln.strip()
        if ln:
            stem = os.path.splitext(os.path.basename(ln.replace("\\", "/")))[0].lower()
            listed[stem] = os.path.normpath(ln)
    out = []
    projs = []
    for p in project_files(aggregate):
        if is_aggregate(p):
            out += drivers(p, seen)
        else:
            projs.append(p)
    per_dir: dict[str, int] = {}
    for p in projs:
        per_dir[os.path.dirname(p)] = per_dir.get(os.path.dirname(p), 0) + 1
    for p in projs:
        d = os.path.dirname(p)
        text = _read(p)
        m = _MAIN.search(text)
        main = os.path.join(d, m.group(1)) if m else None
        runner = None
        if main:
            stem = os.path.splitext(os.path.basename(main))[0].lower()
            runner = listed.get(stem)
            if runner is None:
                e = _EXEC_DIR.search(text)
                exe_dir = _gpr_path(d, e.group(1)) if e else d
                runner = os.path.join(exe_dir, stem + (".exe" if os.name == "nt" else ""))
        units = _read(os.path.join(d, "units.list")).split()
        if per_dir[d] > 1 or not os.path.isdir(d):  # one driver per test: its main names its one test
            files = [main] if main else []
        else:  # one driver per unit: the suite in its directory names the tests
            files = sorted(os.path.join(d, n) for n in os.listdir(d) if n.lower().endswith(".adb"))
        out.append(Driver(project=p, units=[u.lower() for u in units], main=main, runner=runner, files=files))
    return out
