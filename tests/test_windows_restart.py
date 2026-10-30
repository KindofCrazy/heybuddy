"""Windows control signals against a synthetic service, never a real QQ session."""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[1] / "scripts/request-stop.py"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows console lifecycle")
def test_restart_signal_allows_asyncio_finally_cleanup(tmp_path):
    fixture = tmp_path / "service.py"
    ready = tmp_path / "ready"
    closed = tmp_path / "closed"
    fixture.write_text(
        "import asyncio, os, sys\n"
        "from pathlib import Path\n"
        "async def main():\n"
        "    Path(sys.argv[1]).write_text(str(os.getpid()))\n"
        "    try:\n"
        "        await asyncio.Event().wait()\n"
        "    finally:\n"
        "        Path(sys.argv[2]).write_text('closed')\n"
        "try:\n"
        "    asyncio.run(main())\n"
        "except KeyboardInterrupt:\n"
        "    pass\n",
        encoding="utf-8",
    )
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = subprocess.SW_HIDE
    process = subprocess.Popen(
        [sys.executable, str(fixture), str(ready), str(closed)],
        creationflags=subprocess.CREATE_NEW_CONSOLE,
        startupinfo=startup,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    actual_pid = None
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists()
        actual_pid = int(ready.read_text())
        command = [sys.executable, str(HELPER), str(process.pid), str(actual_pid)]
        result = subprocess.run(command, creationflags=subprocess.CREATE_NO_WINDOW, timeout=10)
        assert result.returncode == 0
        process.wait(timeout=10)
        assert closed.read_text() == "closed"
    finally:
        if process.poll() is None:
            # Only the synthetic child created above can be cleaned up this way.
            if actual_pid and actual_pid != process.pid:
                try:
                    os.kill(actual_pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            process.terminate()
            process.wait(timeout=10)
