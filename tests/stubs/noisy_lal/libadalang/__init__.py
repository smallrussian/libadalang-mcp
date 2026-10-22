# Simulates native code writing to stdout while libadalang loads, then failing.
# The server must survive this without corrupting the JSON-RPC stream.
import os
import sys

os.write(1, b"NATIVE GARBAGE ON FD 1\n")
if sys.platform == "win32":
    import ctypes

    k32 = ctypes.windll.kernel32
    h = k32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
    msg = b"NATIVE GARBAGE ON WIN32 STDOUT HANDLE\n"
    written = ctypes.c_ulong(0)
    k32.WriteFile(h, msg, len(msg), ctypes.byref(written), None)
else:
    import ctypes

    libc = ctypes.CDLL(None)
    libc.printf(b"NATIVE GARBAGE VIA PRINTF\n")
    libc.fflush(None)
print("PYTHON PRINT DURING LOAD")
raise ImportError("noisy stub: libadalang.dll failed to load (test stub)")
