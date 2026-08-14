import os
from pathlib import Path

import pytest

from codelux.errors import UnsafePathError
from codelux.safe_files import atomic_write_private, ensure_private_dir


def test_atomic_write_sets_private_mode_and_validates_candidate(tmp_path: Path) -> None:
    root = tmp_path / "private"
    target = root / "settings.json"
    validated = []

    def validate(path: Path) -> None:
        validated.append(path.read_bytes())

    atomic_write_private(target, b"{}", root, validate)

    assert target.read_bytes() == b"{}"
    assert os.stat(target).st_mode & 0o777 == 0o600
    assert os.stat(root).st_mode & 0o777 == 0o700
    assert validated == [b"{}"]


def test_atomic_write_rejects_escape_and_symlink(tmp_path: Path) -> None:
    root = tmp_path / "private"
    root.mkdir()
    with pytest.raises(UnsafePathError, match="escapes"):
        atomic_write_private(tmp_path / "outside", b"x", root)

    real = root / "real"
    real.mkdir()
    link = root / "link"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(UnsafePathError, match="symbolic link"):
        atomic_write_private(link / "settings.json", b"{}", root)


def test_private_dir_skips_redundant_chmod(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    calls = []
    monkeypatch.setattr("codelux.safe_files.os.chmod", lambda *args: calls.append(args))
    ensure_private_dir(root)
    assert calls == []
