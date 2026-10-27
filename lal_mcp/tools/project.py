"""reload_project: rebuild the project view on request, including a GNATtest harness."""

from __future__ import annotations

import os

from lal_mcp.session import Session


def reload_project(sess: Session) -> tuple[str, bool]:
    note = sess.reload("requested")
    files = sess.project_files()
    lines = [note,
             f"project: {sess.rel(sess.project_path)}; watching {len(files)} project file(s) and "
             f"{len(sess._dir_mtimes)} source directories for added/removed files"]
    if sess.harness:
        tests = sorted(sess.rel(f) for f in sess.sources() if f.lower().endswith("-test_data-tests.adb"))
        mode = sess.harness_mode
        kind = "separate drivers" if mode == "separate" else "single driver"
        lines.append(f"GNATtest harness ({kind}): {sess.rel(sess.harness)} loaded as root (it withs the project); "
                     f"{len(tests)} test package(s):")
        lines += [f"  {t}" for t in tests[:20]] + ([f"  ... {len(tests) - 20} more"] if len(tests) > 20 else [])
        if sess.harness_choice:
            lines.append(f"note: {sess.harness_choice}")
        if mode == "separate":
            lines += _drivers_lines(sess)
        lines.append("test_model shows each subprogram's generated test and whether it is still a skeleton"
                     + (", and the driver that runs it" if mode == "separate" else ""))
    elif sess.harness_note:
        lines.append(f"GNATtest: {sess.harness_note}; generate one with gnattest -P {sess.rel(sess.project_path)}")
    lines.append("content edits are re-read automatically; added/removed files, .gpr edits and a newly "
                 "generated GNATtest harness are picked up before the next call")
    if sess.project_errors:
        lines.append("[project warnings] " + "; ".join(sess.project_errors[:3]))
    return "\n".join(lines), False


def _drivers_lines(sess: Session, cap: int = 20) -> list[str]:
    drivers = sess.harness_drivers()
    base = sess.rel(os.path.dirname(sess.harness))
    out = [f"{len(drivers)} driver(s) (also listed in {base}/test_drivers.list); build all with "
           f"gprbuild -P {sess.rel(sess.harness)} (one unit that doesn't compile stops it: then build a "
           "driver's own project), run each runner:"]
    for d in drivers[:cap]:
        runner = sess.rel(d.runner) if d.runner else "unknown (no Main in the driver project)"
        built = "" if d.runner and os.path.isfile(d.runner) else " (not built)"
        out.append(f"  {sess.rel(d.project)}: units {', '.join(d.units) or '?'}; runner {runner}{built}")
    if len(drivers) > cap:
        out.append(f"  ... {len(drivers) - cap} more (test_model names the driver for one subprogram)")
    return out
