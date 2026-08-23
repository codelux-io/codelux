"""Same-agent session sharing for Codex Provider switches."""

import json
import os
import sqlite3
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import Optional, Set

from codelux.errors import ValidationError
from codelux.models import ConfigFile, SessionChange
from codelux.safe_files import atomic_write_private

SHARED_PROVIDER = "custom"


class CodexSessionManager:
    """Prepare reversible logical-provider updates for Codex history."""

    def __init__(self, home: Path) -> None:
        self.home = home.absolute()
        self.root = self.home / ".codex"
        self.sessions_root = self.root / "sessions"
        self.db_path = self.root / "state_5.sqlite"

    def prepare(self, source_providers: Set[str]) -> Optional[SessionChange]:
        source_providers = set(source_providers) - {SHARED_PROVIDER}
        json_changes = []
        if self.sessions_root.is_dir() and not self.sessions_root.is_symlink():
            for path in sorted(self.sessions_root.rglob("*.jsonl")):
                if path.is_symlink() or not path.is_file():
                    raise ValidationError(f"unsafe Codex session file: {path}")
                before = path.read_bytes()
                after = self._rewrite_jsonl(before, source_providers)
                if after != before:
                    logical = "codex/" + str(path.relative_to(self.home / ".codex"))
                    json_changes.append(
                        (
                            ConfigFile(path, before, path.stat().st_mode & 0o777, logical),
                            ConfigFile(path, after, 0o600, logical),
                        )
                    )

        db_before, db_after = self._rewrite_db(source_providers)
        changes = list(json_changes)
        if db_before is not None and db_after is not None and db_after != db_before:
            changes.append(
                (
                    ConfigFile(self.db_path, db_before, 0o600, "codex/state_5.sqlite"),
                    ConfigFile(self.db_path, db_after, 0o600, "codex/state_5.sqlite"),
                )
            )
        if not changes:
            return None
        before_files = tuple(item[0] for item in changes)
        after_files = tuple(item[1] for item in changes)
        sidecars = tuple(
            path
            for path in (
                self.db_path.with_name("state_5.sqlite-wal"),
                self.db_path.with_name("state_5.sqlite-shm"),
            )
            if path.is_file() and not path.is_symlink()
        )
        return SessionChange(before_files, after_files, sidecars)

    def commit(self, change: SessionChange) -> None:
        self._write(change.after, change.cleanup_paths)

    def rollback(self, change: SessionChange) -> None:
        self._write(change.before, change.cleanup_paths, restore=True)

    def _write(
        self, files: Iterable[ConfigFile], cleanup: Iterable[Path], restore: bool = False
    ) -> None:
        if restore:
            for path in cleanup:
                if path.exists() and not path.is_symlink():
                    path.unlink()
        else:
            for path in cleanup:
                if path.exists() and not path.is_symlink():
                    path.unlink()
        for item in files:
            if item.path.is_symlink():
                raise ValidationError(f"unsafe Codex session target: {item.path}")
            atomic_write_private(item.path, item.content, item.path.parent)

    def _rewrite_jsonl(self, raw: bytes, sources: Set[str]) -> bytes:
        lines = raw.splitlines(keepends=True)
        changed = False
        output = []
        for line in lines:
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValidationError("Codex session JSONL is invalid") from exc
            if isinstance(item, dict) and item.get("type") == "session_meta":
                payload = item.get("payload")
                if not isinstance(payload, dict):
                    raise ValidationError("Codex session metadata is invalid")
                provider = payload.get("model_provider")
                if provider in sources:
                    payload["model_provider"] = SHARED_PROVIDER
                    newline = b"\n" if line.endswith(b"\n") else b""
                    line = (
                        json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n"
                    ).encode()
                    if not newline:
                        line = line.rstrip(b"\n")
                    changed = True
            output.append(line)
        return b"".join(output) if changed else raw

    def _rewrite_db(self, sources: Set[str]) -> tuple[Optional[bytes], Optional[bytes]]:
        if not self.db_path.is_file() or self.db_path.is_symlink():
            return None, None
        before = self._db_bytes(self.db_path)
        fd, name = tempfile.mkstemp(prefix="codelux-session-", suffix=".sqlite", dir=self.root)
        os.close(fd)
        temp = Path(name)
        try:
            with sqlite3.connect(temp) as target:
                target.execute("PRAGMA journal_mode=DELETE")
                with sqlite3.connect(self.db_path) as source:
                    source.backup(target)
                cols = target.execute("PRAGMA table_info(threads)").fetchall()
                if not any(row[1] == "model_provider" for row in cols):
                    return before, before
                placeholders = ",".join("?" for _ in sources)
                if sources:
                    target.execute(
                        f"UPDATE threads SET model_provider = ? WHERE model_provider IN ({placeholders})",
                        (SHARED_PROVIDER, *sorted(sources)),
                    )
                target.commit()
            return before, temp.read_bytes()
        except sqlite3.Error as exc:
            raise ValidationError("Codex session database is invalid") from exc
        finally:
            temp.unlink(missing_ok=True)

    @staticmethod
    def _db_bytes(path: Path) -> bytes:
        fd, name = tempfile.mkstemp(prefix="codelux-session-read-", suffix=".sqlite")
        os.close(fd)
        temp = Path(name)
        try:
            with sqlite3.connect(temp) as target, sqlite3.connect(path) as source:
                source.backup(target)
            return temp.read_bytes()
        finally:
            temp.unlink(missing_ok=True)
