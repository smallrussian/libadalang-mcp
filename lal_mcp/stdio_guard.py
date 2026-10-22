"""Keep the JSON-RPC pipes (fd 0 / fd 1) away from native code.

Libadalang / GPR loading is native code that can write straight to fd 1 (or,
on Windows, to the Win32 STD_OUTPUT_HANDLE) and can query the stdin handle.
Call `protect_stdio()` before anything else. It:

- saves private duplicates of the real stdin/stdout for the MCP transport;
- points fd 1 (and the Win32 stdout handle) at stderr, so stray native output
  lands in the log instead of corrupting the protocol stream;
- points fd 0 (and the Win32 stdin handle) at the null device. On Windows,
  synchronous I/O on a pipe is serialized: if native code touches the stdin
  pipe while the transport's reader thread is blocked in ReadFile on it, that
  call waits until the client happens to send more bytes (seen as the GNAT
  runtime initialisation stalling `import libadalang` on the first tool call).
"""

from __future__ import annotations

import io
import os
import sys

_STD_INPUT_HANDLE = -10
_STD_OUTPUT_HANDLE = -11


def _rebind_win32(std_id: int, fd: int) -> None:
    """Point a Win32 standard-handle slot at whatever `fd` now is."""
    if sys.platform != "win32":
        return
    import ctypes
    import msvcrt

    ctypes.windll.kernel32.SetStdHandle(std_id, msvcrt.get_osfhandle(fd))


def protect_stdio() -> tuple[io.TextIOWrapper, io.TextIOWrapper]:
    """Divert fd 0/1 and return (stdin, stdout) text streams on the real pipes.

    The returned streams are the only things that may talk to the client.
    """
    sys.stdout.flush()
    wire_in_fd = os.dup(0)
    wire_out_fd = os.dup(1)

    devnull = os.open(os.devnull, os.O_RDONLY)
    os.dup2(devnull, 0)
    os.close(devnull)
    os.dup2(2, 1)
    _rebind_win32(_STD_INPUT_HANDLE, 0)
    _rebind_win32(_STD_OUTPUT_HANDLE, 1)

    if sys.platform == "win32":
        import msvcrt

        msvcrt.setmode(wire_in_fd, os.O_BINARY)
        msvcrt.setmode(wire_out_fd, os.O_BINARY)
    wire_in = io.TextIOWrapper(os.fdopen(wire_in_fd, "rb", closefd=False), encoding="utf-8", errors="replace")
    wire_out = io.TextIOWrapper(
        os.fdopen(wire_out_fd, "wb", buffering=0, closefd=False),
        encoding="utf-8",
        newline="\n",
        write_through=True,
    )
    # Python-level reads/prints from our own code no longer reach the client.
    sys.stdin = open(os.devnull, encoding="utf-8")
    sys.stdout = sys.stderr
    return wire_in, wire_out
