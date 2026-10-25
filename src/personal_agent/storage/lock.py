"""One local CLI owner per database; OS releases this advisory lock after a crash."""

from pathlib import Path

from personal_agent.core.history import HistoryError
from personal_agent.file_lock import FileLock, FileLockError


class DatabaseLock(FileLock):
    def __init__(self, path: Path) -> None:
        try:
            super().__init__(path)
        except FileLockError:
            raise HistoryError("Database is in use or its lock cannot be opened.") from None
