"""One local CLI owner per database; OS releases this advisory lock after a crash."""

import os
from pathlib import Path

from personal_agent.core.history import HistoryError


class DatabaseLock:
    def __init__(self, path: Path) -> None:
        self._file = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._file = path.open("a+b")
            self._file.seek(0, os.SEEK_END)
            if self._file.tell() == 0:
                self._file.write(b"0")
                self._file.flush()
            self._file.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            if self._file is not None:
                self._file.close()
            raise HistoryError("Database is in use or its lock cannot be opened.") from None

    def close(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None
