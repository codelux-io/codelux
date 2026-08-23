"""Private-directory and atomic-file helpers."""

import os
import tempfile
from pathlib import Path
from typing import Callable, Optional

from codelux.errors import UnsafePathError

FileValidator = Callable[[Path], None]


def ensure_private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise UnsafePathError("private directory must be a real directory")
    if path.stat().st_mode & 0o777 != 0o700:
        os.chmod(path, 0o700)


def atomic_write_private(
    target: Path,
    content: bytes,
    root: Path,
    validator: Optional[FileValidator] = None,
) -> None:
    root = root.absolute()
    target = target.absolute()
    _validate_target(target, root)
    ensure_private_dir(root)
    ensure_private_dir(target.parent)

    fd, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb", closefd=True) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if validator is not None:
            validator(temporary)
        if target.is_symlink():
            raise UnsafePathError("target must not be a symbolic link")
        os.replace(temporary, target)
        os.chmod(target, 0o600)
        directory_fd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary.exists():
            temporary.unlink()


def _validate_target(target: Path, root: Path) -> None:
    try:
        relative = target.relative_to(root)
    except ValueError as exc:
        raise UnsafePathError("target escapes the expected root") from exc
    current = root
    for component in relative.parts[:-1]:
        current = current / component
        if current.is_symlink():
            raise UnsafePathError("target path traverses a symbolic link")
