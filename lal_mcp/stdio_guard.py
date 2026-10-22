"""Keep fd 1 (the JSON-RPC wire) away from C code.

Libadalang / GPR loading is native code that can write straight to fd 1 (or,
on Windows, to the Win32 STD_OUTPUT_HANDLE). Call `protect_stdout()` before
anything else: it saves a private duplicate of the real stdout for the MCP
transport and points fd 1 (and the Win32 handle) at stderr, so stray native
output lands in the log instead of corrupting the protocol stream.
"""

from __future__ import annotations

import io
import os
import sys


def _rebind_win32_stdout() -> None:
    """Point the Win32 STD_OUTPUT_HANDLE slot at whatever fd 1 now is."""
    if sys.platform != "win32":
        return
    import ctypes
    import msvcrt

    STD_OUTPUT_HANDLE = -11
    handle = msvcrt.get_osfhandle(1)
    ctypes.windll.kernel32.SetStdHandle(STD_OUTPUT_HANDLE, handle)


def protect_stdout() -> io.TextIOWrapper:
    """Divert fd 1 to stderr and return a text stream on the real stdout.

    The returned stream is the only thing that may write to the client.
    """
    sys.stdout.flush()
    wire_fd = os.dup(1)
    os.dup2(2, 1)
    _rebind_win32_stdout()
    if sys.platform == "win32":
        import msvcrt

        msvcrt.setmode(wire_fd, os.O_BINARY)
    wire = io.TextIOWrapper(
        os.fdopen(wire_fd, "wb", buffering=0, closefd=False),
        encoding="utf-8",
        newline="\n",
        write_through=True,
    )
    # Python-level prints from our own code now go to stderr too.
    sys.stdout = sys.stderr
    return wire
