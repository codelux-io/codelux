"""Process-level global operation lock for macOS and Linux."""

import fcntl
import os
from pathlib import Path
from types import TracebackType
from typing import Optional, Type

from codelux.errors import LockUnavailableError, UnsafePathError
from codelux.safe_files import ensure_private_dir


class OperationLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._fd: Optional[int] = None

    def __enter__(self) -> "OperationLock":
        ensure_private_dir(self.path.parent)
        if self.path.is_symlink():
            raise UnsafePathError("operation lock must not be a symbolic link")
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(self.path, flags, 0o600)
        os.fchmod(fd, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(fd)
            raise LockUnavailableError("another Codelux operation is in progress") from exc
        self._fd = fd
        return self

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc: Optional[BaseException],
        traceback: Optional[TracebackType],
    ) -> None:
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None
