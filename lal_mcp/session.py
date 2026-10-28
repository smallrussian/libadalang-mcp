"""GPR project / AnalysisContext lifecycle.

One warm AnalysisContext per process, created lazily on the first tool call.
Files are re-parsed when their mtime changes. The whole project view (source
list, unit provider, derived caches) is rebuilt when a project file changes or
Ada files are added to / removed from a source directory (refresh_if_changed),
or on request (reload). All paths shown to the model are workspace-relative
with forward slashes.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

log = logging.getLogger("lal_mcp.session")

ADA_EXTS = (".ads", ".adb", ".ada")
_GPR_COMMENT = re.compile(r"--[^\n]*")
# with "a.gpr", "b";  limited with "c.gpr";  project X extends [all] "d.gpr" is
_GPR_DEPS = re.compile(r'\b(?:limited\s+)?with\s+"[^;]*;|\bextends\s+(?:all\s+)?"[^"]+"', re.I)
# caches other modules attach to the session; cleared on reload
_GPR_PROJECT_FILES = re.compile(r"\bfor\s+Project_Files\s+use\s*\([^)]*\)", re.I)
_DERIVED_CACHES = ("_unit_index", "_buildinfo", "_drivers")


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
    tests_project: str | None = None  # explicit GNATtest harness: test_driver(s).gpr or its directory
    gnattest: bool = True             # auto-detect the GNATtest harness in the object directory


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
        # change detection (see refresh_if_changed)
        self._gpr_mtimes: dict[str, float | None] = {}
        self._dir_mtimes: dict[str, float | None] = {}
        self._dir_names: dict[str, frozenset[str]] = {}
        self._last_check = 0.0
        self.reloads = 0
        # GNATtest: the harness project, when loaded as the root (it withs the project)
        self.harness: str | None = None
        self.harness_note: str | None = None  # why it is not loaded, if it isn't
        self._harness_watch: list[str] = []
        self.harness_choice: str | None = None  # which layout was picked when both exist
        self._drivers = None

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
        (self.lal, self.prj, self.ctx), self.harness, bi = self._build_view()
        self._adopt_buildinfo(bi)
        self._snapshot()
        log.info("project loaded: %d source files%s", len(self.sources()),
                 f" (with GNATtest harness {self.harness})" if self.harness else "")

    @property
    def load_root(self) -> str:
        """The project actually loaded: the GNATtest harness if any, else the configured project."""
        return self.harness or self.project_path

    def _harness_candidate(self):
        """(harness project or None, files to watch, note, BuildInfo or None). Explicit tests_project
        (a harness file or directory) wins; otherwise GNATtest's default location in the project's
        object directory, via gprinspect. Both layouts are looked for: test_drivers.gpr
        (--separate-drivers) and test_driver.gpr (single driver)."""
        from lal_mcp import harness

        if self.cfg.tests_project:
            explicit = os.path.abspath(self.cfg.tests_project)
            path, note = harness.resolve_explicit(explicit)
            watch = harness.candidates(explicit) if os.path.isdir(explicit) else [path]
            return path, watch, note, None
        if not self.cfg.gnattest:
            return None, [], None, None
        try:
            from lal_mcp.buildinfo import BuildInfo

            bi = BuildInfo(self)
        except ToolFailure as exc:
            log.info("GNATtest harness detection off: %s", exc)
            return None, [], None, None
        d = bi.gnattest_harness_dir()
        if not d:
            return None, [], None, bi
        path, note = harness.pick(d)
        return path, harness.candidates(d), note, bi

    @property
    def harness_mode(self) -> str | None:
        """'separate' (aggregate of per-unit/per-test drivers), 'single', or None (no harness loaded)."""
        if not self.harness:
            return None
        from lal_mcp import harness

        return "separate" if harness.is_aggregate(self.harness) else "single"

    def harness_drivers(self):
        """The drivers of a loaded --separate-drivers harness (empty otherwise); cached per view."""
        if self._drivers is None:
            from lal_mcp import harness

            self._drivers = harness.drivers(self.harness) if self.harness_mode == "separate" else []
        return self._drivers

    def _build_view(self):
        """Build the project view: the GNATtest harness as root when it exists and loads,
        else the project itself. Returns ((lal, prj, ctx), harness path or None, BuildInfo)."""
        cand, watch, self.harness_choice, bi = self._harness_candidate()
        self._harness_watch = watch
        self.harness_note = None
        if cand and os.path.isfile(cand):
            errors_before = list(self.project_errors)
            try:
                view = self._build(cand)
                return view, cand, bi
            except ToolFailure as exc:
                self.project_errors = errors_before
                self.harness_note = f"GNATtest harness {self.rel(cand)} not loaded ({exc}); using the project only"
                log.warning("%s", self.harness_note)
        elif cand and self.cfg.tests_project and not os.path.isdir(self.cfg.tests_project):
            # an explicit harness file: name exactly that file, it's what will be watched
            self.harness_note = f"no GNATtest harness yet ({self.rel(cand)}, set by tests_project, does not exist)"
        elif cand:
            d = self.rel(os.path.dirname(cand))
            self.harness_note = (f"no GNATtest harness yet (expected at {d}/test_driver.gpr, "
                                 "or test_drivers.gpr for --separate-drivers)")
        return self._build(self.project_path), None, bi

    def _adopt_buildinfo(self, bi) -> None:
        """Reuse the gprinspect result from harness detection for link_info (same project tree)."""
        if bi is not None:
            self._buildinfo = bi

    def _build(self, root: str | None = None):
        """Load a GPR project and create a fresh analysis context. Doesn't touch self's state
        except project_errors / load_note, so a failed reload leaves the previous view usable."""
        root = root or self.project_path
        try:
            import libadalang as lal
        except Exception as exc:  # ImportError, OSError from a missing DLL/.so
            raise ToolFailure(
                f"libadalang import failed: {last_line(exc)}. "
                "Check PYTHONPATH and PATH/LD_LIBRARY_PATH for the GNAT Pro libadalang install."
            ) from exc
        if not os.path.isfile(root):
            raise ToolFailure(f"project file not found: {root}")
        if "PATH" not in os.environ:
            # GPR's Ada code reads PATH unconditionally; when it is unset it raises an
            # unhandled CONSTRAINT_ERROR that terminates the whole process.
            os.environ["PATH"] = os.defpath
            log.warning("PATH was unset; using %r. Put the GNAT bin directory on PATH.", os.defpath)
        log.info("loading project %s", root)
        try:
            prj = lal.GPRProject(
                root,
                scenario_vars=dict(self.cfg.scenario),
                target=self.cfg.target,
                runtime=self.cfg.runtime,
                print_errors=False,
            )
        except Exception as exc:
            raise ToolFailure(f"cannot load project {root}: {last_line(exc)}") from exc
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
        return lal, prj, ctx

    # ------------------------------------------------------- reload / invalidation

    def project_files(self) -> list[str]:
        """The root .gpr and the project files it withs/extends that can be found (textual scan).
        Installed projects that can't be located are not watched; they rarely change."""
        search = [os.path.dirname(self.load_root), os.path.dirname(self.project_path)]
        for var in ("GPR_PROJECT_PATH", "ADA_PROJECT_PATH"):
            search += [p for p in os.environ.get(var, "").split(os.pathsep) if p]
        gnat = shutil.which("gnat") or shutil.which("gprbuild")
        if gnat:
            prefix = os.path.dirname(os.path.dirname(gnat))
            search += [os.path.join(prefix, "share", "gpr"), os.path.join(prefix, "lib", "gnat")]
        seen: dict[str, str] = {}
        todo = [self.load_root, self.project_path]
        while todo:
            f = os.path.abspath(todo.pop())
            key = norm(f)
            if key in seen or not os.path.isfile(f):
                continue
            seen[key] = f
            try:
                with open(f, encoding="latin-1") as fh:
                    text = _GPR_COMMENT.sub("", fh.read())
            except OSError:
                continue
            clauses = [c.group(0) for c in _GPR_DEPS.finditer(text)]
            clauses += [c.group(0) for c in _GPR_PROJECT_FILES.finditer(text)]  # an aggregate's projects
            for clause in clauses:
                for name in re.findall(r'"([^"]+)"', clause):
                    name = name.replace("\\", "/")  # gnattest writes Windows separators
                    if not name.lower().endswith(".gpr"):
                        name += ".gpr"
                    base = [os.path.dirname(f)] + search
                    hit = next((os.path.join(b, name) for b in base if os.path.isfile(os.path.join(b, name))), None)
                    if hit:
                        todo.append(hit)
        return list(seen.values())

    def _source_dirs(self) -> set[str]:
        return {os.path.dirname(f) for f in self.sources()}

    @staticmethod
    def _stat(path: str) -> float | None:
        try:
            return os.stat(path).st_mtime
        except OSError:
            return None

    def _ada_names(self, d: str) -> frozenset[str]:
        exts = getattr(self, "_watch_exts", ADA_EXTS)
        try:
            return frozenset(n.lower() for n in os.listdir(d) if n.lower().endswith(exts))
        except OSError:
            return frozenset()

    def _snapshot(self) -> None:
        """Record what the current project view was built from."""
        # also watch the suffixes this project's Naming package actually uses (.1.ada, .spc ...)
        self._watch_exts = tuple(sorted(set(ADA_EXTS) | {os.path.splitext(f)[1].lower()
                                                         for f in self.sources() if os.path.splitext(f)[1]}))
        self._gpr_mtimes = {f: self._stat(f) for f in self.project_files()}
        for h in self._harness_watch:  # a harness generated later triggers a reload
            self._gpr_mtimes.setdefault(h, self._stat(h))
        self._dir_mtimes, self._dir_names = {}, {}
        for d in self._source_dirs():
            self._dir_mtimes[d] = self._stat(d)
            self._dir_names[d] = self._ada_names(d)
        self._last_check = time.monotonic()

    def detect_changes(self) -> list[str]:
        """Reasons the project view is out of date: an edited/added/removed .gpr, or Ada files
        added to / removed from a source directory. Content edits of known files don't count
        (those are re-parsed by mtime on their own)."""
        reasons = []
        for f, m in self._gpr_mtimes.items():
            now = self._stat(f)
            if now != m:
                what = "created" if m is None else ("removed" if now is None else "changed")
                reasons.append(f"{os.path.basename(f)} {what}")
        if reasons:
            return reasons
        for d, m in self._dir_mtimes.items():
            now = self._stat(d)
            if now == m:
                continue  # nothing created/deleted/renamed in this directory
            self._dir_mtimes[d] = now  # an atomic save also bumps it: compare names before reloading
            names = self._ada_names(d)
            old = self._dir_names.get(d, frozenset())
            added, removed = names - old, old - names
            if added or removed:
                where = self.rel(d) or "."
                if added:
                    reasons.append(f"{len(added)} file(s) added in {where}")
                if removed:
                    reasons.append(f"{len(removed)} file(s) removed from {where}")
        return reasons

    def reload(self, reason: str) -> str:
        """Rebuild the project view and drop every cache derived from it."""
        old = set(self._source_keys) if self._sources is not None else set()
        try:
            (lal, prj, ctx), harness, bi = self._build_view()
        except ToolFailure as exc:
            self._last_check = time.monotonic()
            raise ToolFailure(f"project reload failed ({reason}): {exc}. Still answering from the previous "
                              "project view; fix the project and the next call retries.") from exc
        self.lal, self.prj, self.ctx = lal, prj, ctx
        self.harness = harness
        self._sources, self._source_keys, self._mtimes = None, set(), {}
        for attr in _DERIVED_CACHES:
            if hasattr(self, attr):
                setattr(self, attr, None)
        self._adopt_buildinfo(bi)
        self._load_error = None
        self._snapshot()
        self.reloads += 1
        new = set(self._source_keys)
        diff = f"{len(self.sources())} sources"
        if self.harness:
            diff += f", GNATtest harness {self.rel(self.harness)}"
            if self.harness_mode == "separate":
                diff += f" (separate drivers: {len(self.harness_drivers())})"
        if old:
            diff += f" (+{len(new - old)} / -{len(old - new)})"
        log.info("project reloaded: %s; %s", reason, diff)
        return f"[project reloaded: {reason}; {diff}]"

    def refresh_if_changed(self, interval: float = 1.0) -> str | None:
        """Called before each tool call: reload when the project view is stale. Returns a note."""
        if self.ctx is None or time.monotonic() - self._last_check < interval:
            return None
        self._last_check = time.monotonic()
        reasons = self.detect_changes()
        if not reasons:
            return None
        return self.reload("; ".join(reasons))

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
