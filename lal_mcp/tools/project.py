"""reload_project: rebuild the project view on request, including a GNATtest harness."""

from __future__ import annotations

from lal_mcp.session import Session


def reload_project(sess: Session) -> tuple[str, bool]:
    note = sess.reload("requested")
    files = sess.project_files()
    lines = [note,
             f"project: {sess.rel(sess.project_path)}; watching {len(files)} project file(s) and "
             f"{len(sess._dir_mtimes)} source directories for added/removed files"]
    if sess.harness:
        tests = sorted(sess.rel(f) for f in sess.sources() if f.lower().endswith("-test_data-tests.adb"))
        lines.append(f"GNATtest harness: {sess.rel(sess.harness)} loaded as root (it withs the project); "
                     f"{len(tests)} test package(s):")
        lines += [f"  {t}" for t in tests[:20]] + ([f"  ... {len(tests) - 20} more"] if len(tests) > 20 else [])
        lines.append("test_model shows each subprogram's generated test and whether it is still a skeleton")
    elif sess.harness_note:
        lines.append(f"GNATtest: {sess.harness_note}; generate one with gnattest -P {sess.rel(sess.project_path)}")
    lines.append("content edits are re-read automatically; added/removed files, .gpr edits and a newly "
                 "generated GNATtest harness are picked up before the next call")
    if sess.project_errors:
        lines.append("[project warnings] " + "; ".join(sess.project_errors[:3]))
    return "\n".join(lines), False
