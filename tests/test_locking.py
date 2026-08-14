from pathlib import Path

import pytest

from codelux.errors import LockUnavailableError, UnsafePathError
from codelux.locking import OperationLock


def test_operation_lock_is_exclusive_and_reusable(tmp_path: Path) -> None:
    path = tmp_path / "operation.lock"
    with OperationLock(path):
        with pytest.raises(LockUnavailableError):
            with OperationLock(path):
                pass

    with OperationLock(path):
        assert path.stat().st_mode & 0o777 == 0o600


def test_operation_lock_rejects_symlinked_parent(tmp_path: Path) -> None:
    actual = tmp_path / "actual"
    actual.mkdir(mode=0o755)
    linked = tmp_path / "linked"
    linked.symlink_to(actual, target_is_directory=True)

    with pytest.raises(UnsafePathError):
        with OperationLock(linked / "operation.lock"):
            pass
    assert actual.stat().st_mode & 0o777 == 0o755
