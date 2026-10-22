"""reload_project: rebuild the project view on request."""

from __future__ import annotations

from lal_mcp.session import Session


def reload_project(sess: Session) -> tuple[str, bool]:
    note = sess.reload("requested")
    files = sess.project_files()
    lines = [note,
             f"project: {sess.rel(sess.project_path)}; watching {len(files)} project file(s) and "
             f"{len(sess._dir_mtimes)} source directories for added/removed files",
             "content edits of known files are re-read automatically; reload covers new or removed "
             "files and .gpr changes the automatic check can't see (e.g. on network drives)"]
    if sess.project_errors:
        lines.append("[project warnings] " + "; ".join(sess.project_errors[:3]))
    return "\n".join(lines), False
