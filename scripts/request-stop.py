"""Send Ctrl+C only to the explicitly verified service console, never force-kill."""

import ctypes
import os
import sys
from ctypes import wintypes


def request_stop(target: int, allowed: set[int]) -> bool:
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.AttachConsole.argtypes = [wintypes.DWORD]
    kernel.AttachConsole.restype = wintypes.BOOL
    kernel.FreeConsole.argtypes = []
    kernel.FreeConsole.restype = wintypes.BOOL
    kernel.GetConsoleProcessList.argtypes = [ctypes.POINTER(wintypes.DWORD), wintypes.DWORD]
    kernel.GetConsoleProcessList.restype = wintypes.DWORD
    kernel.SetConsoleCtrlHandler.argtypes = [ctypes.c_void_p, wintypes.BOOL]
    kernel.SetConsoleCtrlHandler.restype = wintypes.BOOL
    kernel.GenerateConsoleCtrlEvent.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.GenerateConsoleCtrlEvent.restype = wintypes.BOOL
    kernel.FreeConsole()
    if not kernel.AttachConsole(target):
        return False
    try:
        members = (wintypes.DWORD * 16)()
        count = kernel.GetConsoleProcessList(members, len(members))
        if not 0 < count <= len(members):
            return False
        if not set(members[:count]) <= allowed | {os.getpid()}:
            return False
        # Ignore our own broadcast; the target's asyncio.run handles Ctrl+C normally.
        if not kernel.SetConsoleCtrlHandler(None, True):
            return False
        return bool(kernel.GenerateConsoleCtrlEvent(0, 0))
    finally:
        kernel.FreeConsole()


if __name__ == "__main__":
    try:
        targets = {int(value) for value in sys.argv[1:]}
        success = (
            sys.platform == "win32"
            and bool(targets)
            and min(targets) > 0
            and request_stop(int(sys.argv[1]), targets)
        )
    except (ValueError, OSError, AttributeError):
        success = False
    sys.exit(0 if success else 1)
