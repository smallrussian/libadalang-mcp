"""Text helpers shared by tools."""

from __future__ import annotations

import re

_COMMENT = re.compile(r"--[^\n]*")


def one_line(text: str, maxlen: int = 160) -> str:
    """Strip comments, collapse whitespace, cap length."""
    s = " ".join(_COMMENT.sub("", text).split())
    s = s.replace("( ", "(").replace(" )", ")")
    return s if len(s) <= maxlen else s[: maxlen - 3].rstrip() + "..."


def capped_lines(text: str, max_lines: int = 25, max_chars: int = 2000) -> str:
    """The first lines of a declaration's source text, with a note if cut."""
    lines = text.splitlines()
    out = lines[:max_lines]
    s = "\n".join(out)
    if len(s) > max_chars:
        s = s[:max_chars].rstrip()
        return s + "\n  ... [declaration cut at {} chars]".format(max_chars)
    if len(lines) > max_lines:
        s += f"\n  ... [{len(lines) - max_lines} more lines]"
    return s


def dedent_block(text: str, first_col: int) -> str:
    """Re-indent a node's text: its first line starts at column `first_col` in the file."""
    lines = text.splitlines()
    if not lines:
        return ""
    pad = first_col - 1
    rest = []
    for ln in lines[1:]:
        lead = len(ln) - len(ln.lstrip(" "))
        rest.append(ln[min(lead, pad):])
    return "\n".join([lines[0], *rest])
