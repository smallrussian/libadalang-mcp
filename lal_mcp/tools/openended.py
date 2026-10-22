"""Open-ended tools: run_lal, api_help, dump."""

from __future__ import annotations

import ast
import logging
import contextlib
import difflib
import inspect
import io
import re
import signal
import sys
import time
import traceback

from lal_mcp import deps
from lal_mcp.session import Session, ToolFailure, last_line, looks_like_unit_name

log = logging.getLogger("lal_mcp.run_lal")

# ------------------------------------------------------------------ timeout


class RunTimeout(Exception):
    pass


@contextlib.contextmanager
def time_limit(seconds: int):
    """Interrupt Python code after `seconds`.

    Uses SIGALRM where available (Linux/macOS, main thread). Elsewhere (Windows)
    a trace function checks the deadline. Neither can interrupt a single long
    native Libadalang call; the timeout fires when it returns to Python.
    """
    if seconds <= 0:
        yield
        return
    use_alarm = hasattr(signal, "SIGALRM") and _main_thread()
    if use_alarm:
        def on_alarm(signum, frame):
            raise RunTimeout()

        old = signal.signal(signal.SIGALRM, on_alarm)
        signal.setitimer(signal.ITIMER_REAL, seconds)
        try:
            yield
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, old)
    else:
        deadline = time.monotonic() + seconds
        old_trace = sys.gettrace()

        def tracer(frame, event, arg):
            if time.monotonic() > deadline:
                raise RunTimeout()
            return tracer

        sys.settrace(tracer)
        try:
            yield
        finally:
            sys.settrace(old_trace)


def _main_thread() -> bool:
    import threading

    return threading.current_thread() is threading.main_thread()


# ------------------------------------------------------------------ run_lal


def _helpers(sess: Session) -> dict:
    lal = sess.lal

    def unit(name_or_path: str, body: bool = False):
        """AnalysisUnit for a unit name (spec, or body if body=True / no spec) or a file path."""
        s = str(name_or_path).strip()
        if looks_like_unit_name(s):
            u = sess.unit_for_name(s, body=body)
            if u is None and not body:
                u = sess.unit_for_name(s, body=True)
            if u is None:
                raise LookupError(f"unresolved: no unit named {s} in the project")
            return u
        return sess.unit(sess.resolve_path(s))

    def units():
        """All project units (for p_find_all_references / p_find_all_calls)."""
        return sess.all_units()

    def rel(x) -> str:
        """Workspace-relative 'path:line' for a node, or a relative path for a string/unit."""
        if x is None:
            return "None"
        if isinstance(x, lal.AdaNode):
            return f"{sess.rel(x.unit.filename)}:{x.sloc_range.start.line}"
        if isinstance(x, lal.AnalysisUnit):
            return sess.rel(x.filename)
        return sess.rel(str(x))

    return {"lal": lal, "ctx": sess.ctx, "prj": sess.prj, "unit": unit, "units": units, "rel": rel}


def run_lal(sess: Session, code: str) -> tuple[str, bool]:
    if not code.strip():
        raise ToolFailure("code is empty")
    try:
        tree = ast.parse(code, "<run_lal>", "exec")
    except SyntaxError as exc:
        return f"error: SyntaxError: {exc.msg} (line {exc.lineno})", True
    # REPL-like: if the last statement is an expression, print its value.
    last_expr = None
    if tree.body and isinstance(tree.body[-1], ast.Expr):
        last_expr = ast.Expression(tree.body.pop().value)
    body = compile(tree, "<run_lal>", "exec")
    tail = compile(last_expr, "<run_lal>", "eval") if last_expr is not None else None

    env = {"__name__": "__run_lal__", "__builtins__": __builtins__, **_helpers(sess)}
    out = io.StringIO()
    err: str | None = None
    log.debug("run_lal start (%d chars, timeout %ss)", len(code), sess.cfg.timeout)
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out), time_limit(sess.cfg.timeout):
            exec(body, env)
            if tail is not None:
                value = eval(tail, env)
                if value is not None:
                    print(_show(value))
    except RunTimeout:
        err = f"error: timed out after {sess.cfg.timeout}s (narrow the query, e.g. one unit instead of units())"
    except sess.lal.StaleReferenceError:
        raise  # let the session reparse and retry once
    except Exception as exc:
        tb = traceback.extract_tb(exc.__traceback__)
        where = next((f" (snippet line {f.lineno})" for f in reversed(tb) if f.filename == "<run_lal>"), "")
        err = f"error: {last_line(exc)}{where}"
    log.debug("run_lal end: %s", err or "ok")
    text = out.getvalue()
    if err:
        return (text + ("\n" if text and not text.endswith("\n") else "") + err), True
    return (text if text else "(no output; use print())"), False


def _show(value) -> str:
    if isinstance(value, (list, tuple)) and len(value) > 50:
        return repr(list(value[:50]))[:-1] + f", ... {len(value) - 50} more]"
    return repr(value)


# ----------------------------------------------------------------- api_help

_RST = [
    (re.compile(r":py:(?:class|meth|attr|func):`~?([^`]+)`"), r"\1"),
    (re.compile(r":rmlink:`([^`]+)`"), r"RM \1"),
    (re.compile(r"``([^`]+)``"), r"\1"),
]


def _clean_doc(doc: str | None) -> str:
    doc = doc or "(no docstring)"
    for rx, rep in _RST:
        doc = rx.sub(rep, doc)
    return doc.strip()


def _member_sig(owner, name: str) -> str:
    static = inspect.getattr_static(owner, name)
    if isinstance(static, property):
        ret = inspect.signature(static.fget).return_annotation if static.fget else inspect.Signature.empty
        ret_s = "" if ret is inspect.Signature.empty else f" -> {ret}"
        return f"{name}  (property){ret_s}"
    try:
        return f"{name}{inspect.signature(getattr(owner, name))}"
    except (TypeError, ValueError):
        return name


def api_help(sess: Session, name: str) -> tuple[str, bool]:
    try:
        import libadalang as lal
    except Exception as exc:
        raise ToolFailure(f"libadalang import failed: {last_line(exc)}") from exc

    q = name.strip()
    for prefix in ("libadalang.", "lal."):
        if q.startswith(prefix):
            q = q[len(prefix):]
    if not q:
        raise ToolFailure("name is empty")
    parts = q.split(".")
    obj, owner = lal, None
    for i, p in enumerate(parts):
        if not hasattr(obj, p):
            scope = obj if obj is not lal else lal
            cands = [n for n in dir(scope) if not n.startswith("_")]
            close = difflib.get_close_matches(p, cands, n=6, cutoff=0.5)
            where = ".".join(parts[:i]) or "libadalang"
            return (f"unresolved: {where} has no attribute {p}"
                    + (f". Close matches: {', '.join(close)}" if close else "")), True
        owner, obj = obj, getattr(obj, p)

    if inspect.isclass(obj):
        bases = [b.__name__ for b in obj.__mro__[1:] if b is not object]
        out = [f"class {obj.__name__}" + (f"({bases[0]})" if bases else ""),
               _clean_doc(inspect.getdoc(obj))]
        own = [k for k in vars(obj) if not k.startswith("_")]
        fields = [k for k in own if k.startswith("f_")]
        props = [k for k in own if k.startswith("p_")]
        other = [k for k in own if not k.startswith(("f_", "p_"))]
        if fields:
            out.append("fields (syntax children): " + ", ".join(fields))
        if props:
            out.append("properties (semantic): " + ", ".join(props))
        if other:
            out.append("other: " + ", ".join(other))
        if bases and bases[0] not in ("object",):
            n_inh = sum(1 for k in dir(obj) if k.startswith(("f_", "p_")) and k not in own)
            out.append(f"inherits {n_inh} more f_/p_ members from {' -> '.join(bases[:4])}"
                       f" (api_help('{bases[0]}') to list them)")
        subclasses = [c.__name__ for c in obj.__subclasses__()]
        if subclasses:
            out.append("subclasses: " + ", ".join(subclasses[:30]) + (" ..." if len(subclasses) > 30 else ""))
        return "\n".join(out), False

    label = _member_sig(owner, parts[-1]) if owner is not None and inspect.isclass(owner) else q
    if owner is not None and inspect.isclass(owner):
        label = f"{owner.__name__}.{label}"
    elif callable(obj):
        try:
            label = f"{q}{inspect.signature(obj)}"
        except (TypeError, ValueError):
            pass
    static = inspect.getattr_static(owner, parts[-1]) if owner is not None else obj
    doc = inspect.getdoc(static if isinstance(static, property) else obj)
    if not callable(obj) and not isinstance(static, property) and not inspect.isclass(owner):
        return f"{q} = {obj!r}", False
    return f"{label}\n{_clean_doc(doc)}", False


# --------------------------------------------------------------------- dump


def _target_unit(sess: Session, target: str):
    t = target.strip()
    if looks_like_unit_name(t):
        parts = deps.find_parts(sess, t)
        if not parts:
            raise ToolFailure(f"unresolved: no unit named {t} in the project")
        spec = next((p for p in parts if p.kind == "spec"), parts[0])
        return sess.unit(spec.path)
    return sess.unit(sess.resolve_path(t))


def _covering(root, start: tuple[int, int], end: tuple[int, int]) -> list:
    """Smallest node covering the (line, col) range, or the items of a list that overlap it."""

    def pos(sloc) -> tuple[int, int]:
        return (sloc.line, sloc.column)

    node = root
    while True:
        kids = [c for c in node.children if c is not None and c.text]
        inside = [c for c in kids if pos(c.sloc_range.start) <= start and pos(c.sloc_range.end) >= end]
        if len(inside) == 1:
            node = inside[0]
            continue
        if node is root or node.is_list_type:  # a list: return just the items in range
            overlap = [c for c in kids if pos(c.sloc_range.end) >= start and pos(c.sloc_range.start) <= end]
            return overlap or [node]
        return [node]


def _dump_node(node, depth: int, max_depth: int, label: str, out: list[str]) -> None:
    ind = "  " * depth
    if not node.is_token_node and not node.text:  # Absent/Unspecified placeholder nodes
        out.append(f"{ind}{label}{node.kind_name}")
        return
    sr = node.sloc_range
    loc = f"{sr.start.line}:{sr.start.column}-{sr.end.line}:{sr.end.column}"
    head = f"{ind}{label}{node.kind_name} {loc}"
    if node.is_token_node:
        out.append(f'{head} "{node.text}"')
        return
    fields = list(node.iter_fields())
    if not node.is_list_type and all(v is None for _, v in fields):  # leaf such as SubpKindProcedure
        out.append(f'{head} "{" ".join(node.text.split())[:60]}"')
        return
    if depth >= max_depth:
        n = sum(1 for _, v in fields if v is not None)
        snippet = " ".join(node.text.split())
        snippet = snippet if len(snippet) <= 60 else snippet[:57] + "..."
        out.append(f'{head} ... [{n} children] "{snippet}"')
        return
    out.append(head)
    for fname, value in fields:
        flabel = "" if node.is_list_type else f"{fname}: "
        if value is None:
            if not node.is_list_type:
                out.append(f"{ind}  {fname}: None")
            continue
        _dump_node(value, depth + 1, max_depth, flabel, out)


def dump(sess: Session, target: str, start_line: int, end_line: int, max_depth: int) -> tuple[str, bool]:
    if start_line < 1 or end_line < start_line:
        raise ToolFailure("need 1 <= start_line <= end_line")
    if max_depth < 1:
        raise ToolFailure("max_depth must be >= 1")
    unit = _target_unit(sess, target)
    if unit.root is None:
        return f"unresolved: {sess.rel(unit.filename)} could not be parsed", True
    n_lines = unit.text.count("\n") + 1
    if start_line > n_lines:
        raise ToolFailure(f"start_line {start_line} out of range: {sess.rel(unit.filename)} has {n_lines} lines")
    end_line = min(end_line, n_lines)
    lines = unit.text.splitlines()
    first = lines[start_line - 1] if start_line <= len(lines) else ""
    last = lines[end_line - 1] if end_line <= len(lines) else ""
    scol = len(first) - len(first.lstrip()) + 1
    ecol = len(last.rstrip()) + 1
    nodes = _covering(unit.root, (start_line, scol), (end_line, ecol))
    out = [f"{sess.rel(unit.filename)} lines {start_line}-{end_line}: {len(nodes)} node(s), "
           f"depth <= {max_depth} (fields: f_*, see api_help)"]
    if sess.has_errors(unit):
        out.insert(0, "[approximate] file has parse errors; tree may contain error-recovery nodes")
    for n in nodes:
        _dump_node(n, 0, max_depth, "", out)
    return "\n".join(out), False
