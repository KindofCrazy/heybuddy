"""Small local process lock shared by conversation and memory stores."""

import os
from pathlib import Path
from typing import Self


class FileLockError(Exception):
    pass


class FileLock:
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
            self.close()
            raise FileLockError("Local store is busy or its lock is unavailable.") from None

    def close(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_) -> None:
        self.close()
