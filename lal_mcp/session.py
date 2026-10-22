"""GPR project / AnalysisContext lifecycle.

One warm AnalysisContext per process, created lazily on the first tool call.
Files are re-parsed when their mtime changes. All paths shown to the model
are workspace-relative with forward slashes.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

log = logging.getLogger("lal_mcp.session")

ADA_EXTS = (".ads", ".adb", ".ada")


class ToolFailure(Exception):
    """An error the model should see as an `isError` result."""


@dataclass
class Config:
    project: str
    workspace: str | None = None
    scenario: dict[str, str] = field(default_factory=dict)
    target: str | None = None
    runtime: str | None = None
    max_chars: int = 8000
    timeout: int = 20


def norm(path: str) -> str:
    """Canonical key for a file path (absolute, normalized, case-folded on Windows)."""
    return os.path.normcase(os.path.realpath(path))


def last_line(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}".strip()
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return lines[-1] if lines else type(exc).__name__


def cap(text: str, max_chars: int, hint: str = "narrow the query") -> str:
    """Hard-cap a response, cutting at a line boundary, and say so."""
    if len(text) <= max_chars:
        return text
    note = f"\n[truncated: {len(text)} chars total, showing first ~{max_chars}; {hint}]"
    budget = max(0, max_chars - len(note))
    cut = text.rfind("\n", 0, budget)
    if cut < budget // 2:
        cut = budget
    return text[:cut] + note


class Session:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.project_path = os.path.abspath(cfg.project)
        self.workspace = os.path.abspath(cfg.workspace or os.path.dirname(self.project_path))
        self.lal: Any = None
        self.prj: Any = None
        self.ctx: Any = None
        self.project_errors: list[str] = []
        self._sources: list[str] | None = None  # absolute paths, project order
        self._source_keys: set[str] = set()
        self._mtimes: dict[str, float] = {}
        self._load_error: str | None = None
        self.load_note: str | None = None  # shown once, on the first tool response after loading

    # ------------------------------------------------------------------ load

    @property
    def loaded(self) -> bool:
        return self.ctx is not None

    def ensure_loaded(self) -> None:
        if self.ctx is not None:
            return
        if self._load_error is not None:
            raise ToolFailure(self._load_error)
        try:
            self._load()
        except ToolFailure as exc:
            self._load_error = str(exc)
            raise

    def _load(self) -> None:
        try:
            import libadalang as lal
        except Exception as exc:  # ImportError, OSError from a missing DLL/.so
            raise ToolFailure(
                f"libadalang import failed: {last_line(exc)}. "
                "Check PYTHONPATH and PATH/LD_LIBRARY_PATH for the GNAT Pro libadalang install."
            ) from exc
        if not os.path.isfile(self.project_path):
            raise ToolFailure(f"project file not found: {self.project_path}")
        if "PATH" not in os.environ:
            # GPR's Ada code reads PATH unconditionally; when it is unset it raises an
            # unhandled CONSTRAINT_ERROR that terminates the whole process.
            os.environ["PATH"] = os.defpath
            log.warning("PATH was unset; using %r. Put the GNAT bin directory on PATH.", os.defpath)
        log.info("loading project %s", self.project_path)
        try:
            prj = lal.GPRProject(
                self.project_path,
                scenario_vars=dict(self.cfg.scenario),
                target=self.cfg.target,
                runtime=self.cfg.runtime,
                print_errors=False,
            )
        except Exception as exc:
            raise ToolFailure(f"cannot load project {self.project_path}: {last_line(exc)}") from exc
        self.project_errors = list(prj.errors)
        for err in self.project_errors:
            log.warning("project: %s", err)
        if self.project_errors:
            shown = "\n  ".join(self.project_errors[:5])
            more = f"\n  ... {len(self.project_errors) - 5} more" if len(self.project_errors) > 5 else ""
            self.load_note = (self.load_note or "") + (
                f"[project warnings, results may be incomplete]\n  {shown}{more}\n"
                "  (\"can't find a toolchain\" means the GNAT bin directory is not on the server's PATH,"
                " so runtime units such as Ada.Text_IO will not resolve)\n"
            )
        try:
            ctx = prj.create_context()
        except Exception as exc:
            raise ToolFailure(f"cannot create analysis context: {last_line(exc)}") from exc
        self.lal, self.prj, self.ctx = lal, prj, ctx
        log.info("project loaded: %d source files", len(self.sources()))

    # --------------------------------------------------------------- sources

    def sources(self) -> list[str]:
        """Absolute paths of the project's Ada sources (root + non-externally-built deps)."""
        if self._sources is None:
            files = [os.path.abspath(f) for f in self.prj.source_files(self.lal.SourceFilesMode.default)]
            self._sources = files
            self._source_keys = {norm(f) for f in files}
        return self._sources

    def in_project(self, path: str) -> bool:
        self.sources()
        return norm(path) in self._source_keys

    # ----------------------------------------------------------------- paths

    def rel(self, path: str) -> str:
        """Workspace-relative path with forward slashes (absolute if outside)."""
        if not path:
            return "<unknown>"
        ap = os.path.abspath(path)
        try:
            r = os.path.relpath(ap, self.workspace)
        except ValueError:  # different drive on Windows
            return ap.replace("\\", "/")
        if r.startswith(".."):
            return ap.replace("\\", "/")
        return r.replace("\\", "/")

    def resolve_path(self, path: str) -> str:
        """Turn a user-supplied file path (relative to workspace, or absolute) into an absolute one."""
        p = path.strip().strip('"')
        cand = p if os.path.isabs(p) else os.path.join(self.workspace, p)
        if os.path.isfile(cand):
            return os.path.abspath(cand)
        # Bare file name: look it up among the project sources.
        if not any(sep in p for sep in "/\\"):
            hits = [f for f in self.sources() if os.path.normcase(os.path.basename(f)) == os.path.normcase(p)]
            if len(hits) == 1:
                return hits[0]
            if len(hits) > 1:
                raise ToolFailure(
                    f"ambiguous file name {p}: " + ", ".join(self.rel(h) for h in hits[:10])
                )
        raise ToolFailure(f"file not found: {p} (paths are relative to the workspace {self.rel(self.workspace) or '.'})")

    # ----------------------------------------------------------------- units

    def unit(self, path: str):
        """AnalysisUnit for an absolute path; re-parses when the file changed on disk."""
        key = norm(path)
        try:
            mtime = os.path.getmtime(path)
        except OSError as exc:
            raise ToolFailure(f"cannot read {self.rel(path)}: {last_line(exc)}") from exc
        prev = self._mtimes.get(key)
        reparse = prev is not None and prev != mtime
        self._mtimes[key] = mtime
        if reparse:
            log.info("reparse %s", self.rel(path))
        return self.ctx.get_from_file(path, reparse=reparse)

    def unit_for_name(self, name: str, body: bool):
        """AnalysisUnit for a unit name, or None if the project has no such unit part."""
        lal = self.lal
        kind = lal.AnalysisUnitKind.unit_body if body else lal.AnalysisUnitKind.unit_specification
        # Libadalang maps any string to some file name, so validate here.
        if not UNIT_NAME_RE.match(name.strip()):
            raise ToolFailure(f"invalid unit name: {name!r} (expected e.g. Pkg.Child)")
        name = name.strip()
        try:
            u = self.ctx.get_from_provider(name, kind)
        except lal.InvalidUnitNameError as exc:
            raise ToolFailure(f"invalid unit name: {name!r}") from exc
        if u is None or not u.filename or not os.path.isfile(u.filename):
            return None
        # Re-fetch through the mtime check so edits are picked up.
        return self.unit(u.filename)

    def all_units(self) -> list:
        """Units for every project source (used for reference searches)."""
        return [self.unit(f) for f in self.sources()]

    @staticmethod
    def has_errors(unit) -> bool:
        return bool(unit.diagnostics)

    def diag_lines(self, unit, limit: int = 3) -> list[str]:
        out = []
        for d in list(unit.diagnostics)[:limit]:
            sl = d.sloc_range.start
            out.append(f"{self.rel(unit.filename)}:{sl.line}:{sl.column}: {d.message}")
        return out

    # ---------------------------------------------------------------- helpers

    def loc(self, node) -> str:
        return f"{self.rel(node.unit.filename)}:{node.sloc_range.start.line}"

    def line_text(self, unit, line: int) -> str:
        try:
            return unit.text.splitlines()[line - 1].strip()
        except Exception:
            return ""

    def with_retry(self, fn: Callable[[], Any]) -> Any:
        """Run fn; on StaleReferenceError drop the mtime cache and retry once."""
        try:
            return fn()
        except self.lal.StaleReferenceError:
            log.info("stale reference, retrying after reparse")
            self._mtimes.clear()
            return fn()


UNIT_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z][A-Za-z0-9_]*)*$")


def looks_like_unit_name(s: str) -> bool:
    return bool(UNIT_NAME_RE.match(s.strip())) and not s.strip().lower().endswith(ADA_EXTS)


def first_lines(text: str, n: int) -> Iterable[str]:
    return text.splitlines()[:n]
