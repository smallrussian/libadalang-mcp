"""Build/link metadata from the GPR toolchain: which project owns a file, which
libraries and C/C++ sources define a link symbol.

Sources of truth, all from the GNAT installation already on PATH:
  gprinspect --display=json   project tree: with clauses, languages, source dirs,
                              library files, Externally_Built, Linker_Options
  nm -g --defined-only        symbols defined by a library (.a/.lib/.so/.dll)
  C/C++ sources of the tree   function/variable definitions: a text search, so
                              results are labeled approximate

Nothing is compiled or written.
"""

from __future__ import annotations

import glob
import json
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field

from lal_mcp.session import Session, ToolFailure, last_line, norm

log = logging.getLogger("lal_mcp.buildinfo")

C_EXTS = (".c", ".h", ".cc", ".cpp", ".cxx", ".hh", ".hpp", ".hxx", ".C")
FOREIGN_LANGS = {"c", "c++", "cpp"}


@dataclass
class Proj:
    name: str
    file: str
    kind: str
    externally_built: bool
    languages: list[str]
    source_dirs: list[str]  # absolute
    library_file: str | None
    linker_options: list[str]
    imports: list[str] = field(default_factory=list)  # project files this one withs
    imported_by: list[str] = field(default_factory=list)
    aggregated: list[str] = field(default_factory=list)  # an aggregate project's Project_Files
    object_dir: str | None = None
    gnattest_harness_dir: str | None = None  # package Gnattest'Harness_Dir, relative to object_dir


def _attr(p: dict, name: str):
    for a in p.get("attributes", []):
        if a.get("name") == name:
            return a.get("value", a.get("values"))
    return None


def _pkg_attr(p: dict, pkg: str, name: str):
    for k in p.get("packages", []):
        if k.get("name") == pkg:
            for a in k.get("attributes", []):
                if a.get("name") == name:
                    return a.get("value", a.get("values"))
    return None


def _as_list(v) -> list[str]:
    if v is None:
        return []
    if isinstance(v, list):
        return [str(x) for x in v]
    s = str(v)
    if s.startswith("[") and s.endswith("]"):  # some list values arrive as their textual image
        return [x.strip().strip("'\"") for x in s[1:-1].split(",") if x.strip()]
    return [s]


def _id_to_file(pid: str) -> str:
    """gprinspect project ids: `<file` for imported projects, `$file` for aggregated ones."""
    return pid[1:] if pid[:1] in ("<", "$") else pid


class BuildInfo:
    def __init__(self, sess: Session):
        self.sess = sess
        self.projects: list[Proj] = []
        self.root: Proj | None = None
        self.warnings: list[str] = []
        self._nm_cache: dict[str, set[str] | None] = {}
        self._c_files: list[tuple[str, Proj]] | None = None
        self._load()

    # ------------------------------------------------------------- gprinspect

    def _load(self) -> None:
        exe = shutil.which("gprinspect")
        if not exe:
            raise ToolFailure("unresolved: gprinspect not found on PATH (it ships with GNAT Pro's bin directory); "
                              "build/link metadata is unavailable")
        cfg = self.sess.cfg
        cmd = [exe, "-P", self.sess.project_path, "--all", "--display=json", "-r"]
        cmd += [f"-X{k}={v}" for k, v in cfg.scenario.items()]
        if cfg.target:
            cmd.append(f"--target={cfg.target}")
        if cfg.runtime:
            cmd.append(f"--RTS={cfg.runtime}")
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL,
                               cwd=os.path.dirname(self.sess.project_path))
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ToolFailure(f"unresolved: gprinspect failed: {last_line(exc)}") from exc
        try:
            data = json.loads(p.stdout)
        except ValueError as exc:
            raise ToolFailure("unresolved: gprinspect returned no JSON: "
                              + (p.stderr.strip().splitlines() or ["(no message)"])[-1]) from exc
        for m in data.get("messages", {}).get("tree", []):
            self.warnings.append(str(m.get("message", m)) if isinstance(m, dict) else str(m))
        for entry in data.get("projects", []):
            pr = entry["project"]
            base = pr.get("directory", "")
            dirs = [os.path.normpath(d if os.path.isabs(d) else os.path.join(base, d))
                    for d in _as_list(pr.get("source-directories") or _attr(entry, "Source_Dirs"))]
            self.projects.append(Proj(
                name=pr.get("name", "?"), file=os.path.normpath(pr.get("file-name", "")), kind=pr.get("kind", "?"),
                externally_built=str(_attr(entry, "Externally_Built") or "false").lower() == "true",
                languages=[x.lower() for x in _as_list(_attr(entry, "Languages"))],
                source_dirs=dirs, library_file=pr.get("library-file-name"),
                linker_options=_as_list(_pkg_attr(entry, "Linker", "Linker_Options")),
                imports=[os.path.normpath(_id_to_file(x)) for x in entry.get("imports", []) or []],
                imported_by=[os.path.normpath(_id_to_file(x)) for x in entry.get("imported-by", []) or []],
                aggregated=[os.path.normpath(_id_to_file(x)) for x in entry.get("aggregated", []) or []],
                object_dir=os.path.normpath(pr["object-directory"]) if pr.get("object-directory") else None,
                gnattest_harness_dir=(_as_list(_pkg_attr(entry, "Gnattest", "Harness_Dir")) or [None])[0],
            ))
        root_id = data.get("tree", {}).get("root-project", {}).get("id", "")
        root_file = norm(_id_to_file(root_id)) if root_id else norm(self.sess.project_path)
        self.root = next((p for p in self.projects if norm(p.file) == root_file), None)

    # -------------------------------------------------------------- projects

    def project_of(self, path: str) -> Proj | None:
        """The project whose source directory holds `path` (deepest match wins)."""
        key = norm(path)
        best, best_len = None, -1
        for p in self.projects:
            for d in p.source_dirs:
                dk = norm(d)
                if (key.startswith(dk + os.sep) or os.path.dirname(key) == dk) and len(dk) > best_len:
                    best, best_len = p, len(dk)
        return best

    def gnattest_harness_dir(self) -> str | None:
        """Where GNATtest puts the root project's harness: <object dir>/<Harness_Dir or gnattest/harness>."""
        if self.root is None or not self.root.object_dir:
            return None
        sub = self.root.gnattest_harness_dir or os.path.join("gnattest", "harness")
        return os.path.normpath(sub if os.path.isabs(sub) else os.path.join(self.root.object_dir, sub))

    def gnattest_harness(self) -> str | None:
        """The harness project in that directory: test_drivers.gpr (--separate-drivers) or
        test_driver.gpr (single driver, also the path to expect when there is none yet)."""
        from lal_mcp import harness

        d = self.gnattest_harness_dir()
        return harness.pick(d)[0] if d else None

    def withed_by_root(self, proj: Proj) -> str:
        """'root project', 'direct', 'aggregated', 'indirect' or 'no' for the root project's closure.
        A project an aggregate root lists in Project_Files (or anything those with) is part of
        the build, so it is reachable even though nothing withs it."""
        if self.root is None:
            return "unknown"
        if norm(proj.file) == norm(self.root.file):
            return "root project"
        if any(norm(f) == norm(proj.file) for f in self.root.imports):
            return "direct"
        if any(norm(f) == norm(proj.file) for f in self.root.aggregated):
            return "aggregated by the root"
        seen, todo = set(), list(self.root.imports) + list(self.root.aggregated)
        while todo:
            f = norm(todo.pop())
            if f in seen:
                continue
            seen.add(f)
            if f == norm(proj.file):
                return "indirect"
            p = next((q for q in self.projects if norm(q.file) == f), None)
            if p:
                todo += p.imports + p.aggregated
        return "no"

    # ----------------------------------------------------- symbol ownership

    def c_files(self) -> list[tuple[str, Proj]]:
        if self._c_files is None:
            out = []
            for p in self.projects:
                if not (set(p.languages) & FOREIGN_LANGS):
                    continue
                for d in p.source_dirs:
                    pattern = os.path.join(d.rstrip("*" + os.sep), "**") if d.endswith("**") else d
                    for f in glob.glob(os.path.join(pattern, "*"), recursive=d.endswith("**")):
                        if f.endswith(C_EXTS) and os.path.isfile(f):
                            out.append((os.path.normpath(f), p))
            self._c_files = out
        return self._c_files

    def c_definitions(self, symbol: str) -> list[tuple[str, int, Proj, str]]:
        """(file, line, project, kind) where C/C++ text defines `symbol`. Text search: approximate."""
        rx = re.compile(r"\b" + re.escape(symbol) + r"\b")
        hits = []
        for path, proj in self.c_files():
            try:
                with open(path, encoding="latin-1") as f:
                    text = f.read()
            except OSError:
                continue
            for m in rx.finditer(text):
                after = text[m.end():m.end() + 400]
                before = text[max(0, m.start() - 200):m.start()]
                line = text.count("\n", 0, m.start()) + 1
                prev_stmt = before.split(";")[-1].split("}")[-1]
                if "extern" in prev_stmt.split() or "#define" in prev_stmt:
                    continue
                stripped = after.lstrip()
                if stripped.startswith("("):  # function: definition if '{' comes before ';'
                    brace, semi = after.find("{"), after.find(";")
                    if brace != -1 and (semi == -1 or brace < semi):
                        hits.append((path, line, proj, "function definition"))
                elif stripped[:1] in ("=", ";", "[") and re.search(r"[\w\*]\s*$", prev_stmt.strip() or "x") \
                        and prev_stmt.strip() and "(" not in prev_stmt and "return" not in prev_stmt.split():
                    hits.append((path, line, proj, "variable definition"))
        return hits

    def nm_symbols(self, lib: str) -> set[str] | None:
        """Global symbols defined by a library, or None if it can't be read."""
        if lib in self._nm_cache:
            return self._nm_cache[lib]
        syms: set[str] | None = None
        exe = shutil.which("nm")
        if exe and os.path.isfile(lib):
            try:
                p = subprocess.run([exe, "-g", "--defined-only", lib], capture_output=True, text=True,
                                   timeout=120, stdin=subprocess.DEVNULL, errors="replace")
                syms = set()
                for ln in p.stdout.splitlines():
                    parts = ln.split()
                    if len(parts) >= 3 and parts[-2] not in ("U", "w", "v"):
                        syms.add(parts[-1])
            except (OSError, subprocess.TimeoutExpired) as exc:
                log.warning("nm %s: %s", lib, last_line(exc))
        self._nm_cache[lib] = syms
        return syms

    def candidate_libraries(self, runtime_dir: str | None) -> list[tuple[str, str]]:
        """(library file, owner) to search for symbols: project libraries, then the GNAT runtime."""
        libs = [(p.library_file, f"project {p.name}") for p in self.projects
                if p.library_file and os.path.isfile(p.library_file)]
        if runtime_dir:
            for name in ("libgnat.a", "libgnarl.a"):
                f = os.path.join(runtime_dir, name)
                if os.path.isfile(f):
                    libs.append((f, "GNAT runtime"))
        return libs
