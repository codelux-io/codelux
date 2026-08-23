"""Phase C synchronization primitives: safe collection, manifests and encryption."""

import hashlib
import io
import json
import secrets
import sqlite3
import struct
import tarfile
import tempfile
import time
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Optional, Union

from codelux.errors import CodeluxError, ValidationError
from codelux.models import FileState, Manifest, ManifestFile, OperationState
from codelux.safe_files import atomic_write_private, ensure_private_dir
from codelux.snapshots import SnapshotStore, _logical_target

MAGIC = b"CDLXSYNC"
VERSION = 1
MAX_FILE = 256 * 1024 * 1024
MAX_FILES = 100_000
MAX_TOTAL = 4 * 1024 * 1024 * 1024
SCRYPT_MAXMEM = 256 * 1024 * 1024
SELECTIONS = {"config", "sessions", "providers"}
SYNC_STATE_SCHEMA = 1
SQLITE_BACKUP_ATTEMPTS = 10
SQLITE_BACKUP_BACKOFF_SECONDS = 0.1


@dataclass(frozen=True)
class SyncFile:
    path: str
    mode: int
    size: int
    sha256: str

    def to_dict(self) -> dict:
        return {"path": self.path, "mode": self.mode, "size": self.size, "sha256": self.sha256}


@dataclass(frozen=True)
class _PreparedWrite:
    logical_path: str
    target: Path
    incoming: bytes
    before: Optional[bytes]
    source_mode: int
    target_mode: int


@dataclass(frozen=True)
class SyncManifest:
    transfer_id: str
    created_at: str
    source_machine_id: str
    selection: tuple[str, ...]
    includes_keys: bool
    files: tuple[SyncFile, ...]
    schema_version: int = 1

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "transfer_id": self.transfer_id,
            "created_at": self.created_at,
            "source_machine_id": self.source_machine_id,
            "selection": list(self.selection),
            "includes_keys": self.includes_keys,
            "files": [item.to_dict() for item in self.files],
        }

    @classmethod
    def from_dict(cls, data: Any) -> "SyncManifest":
        if not isinstance(data, dict) or set(data) != {
            "schema_version",
            "transfer_id",
            "created_at",
            "source_machine_id",
            "selection",
            "includes_keys",
            "files",
        }:
            raise ValidationError("sync manifest is invalid")
        if data["schema_version"] != 1 or not isinstance(data["includes_keys"], bool):
            raise ValidationError("sync manifest is invalid")
        for field in ("transfer_id", "created_at", "source_machine_id"):
            if not isinstance(data[field], str) or not data[field]:
                raise ValidationError("sync manifest is invalid")
        try:
            parsed = datetime.fromisoformat(data["created_at"].replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValidationError("sync manifest is invalid") from exc
        if parsed.tzinfo is None:
            raise ValidationError("sync manifest is invalid")
        selection = data["selection"]
        if (
            not isinstance(selection, list)
            or not selection
            or any(not isinstance(item, str) for item in selection)
            or tuple(selection) != tuple(sorted(set(selection)))
            or not set(selection).issubset(SELECTIONS)
        ):
            raise ValidationError("sync manifest is invalid")
        raw_files = data["files"]
        if not isinstance(raw_files, list) or len(raw_files) > MAX_FILES:
            raise ValidationError("sync manifest is invalid")
        files = []
        total = 0
        seen = set()
        for item in raw_files:
            if not isinstance(item, dict) or set(item) != {"path", "mode", "size", "sha256"}:
                raise ValidationError("sync manifest is invalid")
            path = item["path"]
            mode = item["mode"]
            size = item["size"]
            digest = item["sha256"]
            if not isinstance(path, str) or path in seen:
                raise ValidationError("sync manifest is invalid")
            _safe_relative(path)
            if (
                not isinstance(mode, int)
                or isinstance(mode, bool)
                or mode & 0o777 != mode
                or not isinstance(size, int)
                or isinstance(size, bool)
                or size < 0
                or size > MAX_FILE
                or not isinstance(digest, str)
                or len(digest) != 64
                or any(char not in "0123456789abcdef" for char in digest)
            ):
                raise ValidationError("sync manifest is invalid")
            total += size
            if total > MAX_TOTAL:
                raise ValidationError("sync manifest is invalid")
            seen.add(path)
            files.append(SyncFile(path, mode, size, digest))
        return cls(
            data["transfer_id"],
            data["created_at"],
            data["source_machine_id"],
            tuple(selection),
            data["includes_keys"],
            tuple(files),
            1,
        )


def machine_id(root: Path) -> str:
    path = root / "machine-id"
    ensure_private_dir(root)
    if path.is_symlink():
        raise ValidationError("machine-id must not be a symbolic link")
    if path.is_file():
        value = path.read_text().strip()
        if len(value) < 16:
            raise ValidationError("machine-id is invalid")
        return value
    value = secrets.token_hex(16)
    atomic_write_private(path, (value + "\n").encode(), root)
    return value


def rotate_machine_id(root: Path) -> str:
    ensure_private_dir(root)
    path = root / "machine-id"
    state = root / "sync-state.json"
    if path.is_symlink() or state.is_symlink():
        raise ValidationError("sync identity path is unsafe")
    value = secrets.token_hex(16)
    atomic_write_private(path, (value + "\n").encode(), root)
    if state.exists():
        state.unlink()
    return value


def _state_path(root: Path) -> Path:
    return root / "sync-state.json"


def load_sync_state(root: Path) -> dict[str, Any]:
    path = _state_path(root)
    if path.is_symlink():
        raise ValidationError("sync-state.json is unsafe")
    if not path.exists():
        return {"schema_version": SYNC_STATE_SCHEMA, "baselines": {}}
    if not path.is_file():
        raise ValidationError("sync-state.json is unsafe")
    try:
        data = json.loads(path.read_bytes())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError("sync-state.json is invalid") from exc
    if (
        not isinstance(data, dict)
        or set(data) != {"schema_version", "baselines"}
        or data["schema_version"] != SYNC_STATE_SCHEMA
        or not isinstance(data["baselines"], dict)
    ):
        raise ValidationError("sync-state.json is invalid")
    return data


def _baseline_key(remote_id: str, selection: Sequence[str]) -> str:
    return remote_id + ":" + ",".join(sorted(set(selection)))


def _baseline_bytes(root: Path, manifest: SyncManifest) -> bytes:
    state = load_sync_state(root)
    state["baselines"][_baseline_key(manifest.source_machine_id, manifest.selection)] = {
        "transfer_id": manifest.transfer_id,
        "files": {item.path: item.sha256 for item in manifest.files},
    }
    return (json.dumps(state, sort_keys=True, indent=2) + "\n").encode()


def save_baseline(root: Path, manifest: SyncManifest) -> None:
    atomic_write_private(_state_path(root), _baseline_bytes(root, manifest), root)


def reset_baseline(root: Path, remote_id: str, selection: Optional[Sequence[str]] = None) -> bool:
    state = load_sync_state(root)
    prefix = remote_id + ":"
    keys = (
        [
            key
            for key in state["baselines"]
            if key == _baseline_key(remote_id, selection)
            if selection is not None
        ]
        if selection is not None
        else [key for key in state["baselines"] if key.startswith(prefix)]
    )
    for key in keys:
        del state["baselines"][key]
    atomic_write_private(
        _state_path(root), (json.dumps(state, sort_keys=True, indent=2) + "\n").encode(), root
    )
    return bool(keys)


def _safe_relative(path: str) -> PurePosixPath:
    candidate = PurePosixPath(path)
    if candidate.is_absolute() or ".." in candidate.parts or not candidate.parts:
        raise ValidationError("sync path is unsafe")
    return candidate


def _claude_project_slug(project_root: Path) -> str:
    resolved = project_root.expanduser().absolute()
    return "-" + "-".join(part for part in resolved.parts if part not in ("/", ""))


def map_claude_sessions(
    manifest: SyncManifest, files: Mapping[str, bytes], project_roots: Mapping[str, Path]
) -> tuple[SyncManifest, dict[str, bytes]]:
    if not project_roots or "sessions" not in manifest.selection:
        return manifest, dict(files)
    claude_paths = [
        entry.path for entry in manifest.files if entry.path.startswith("claude/projects/")
    ]
    source_slugs = {path.split("/", 3)[2] for path in claude_paths}
    if not source_slugs or set(project_roots) != source_slugs:
        raise ValidationError("Claude session project mapping is incomplete")
    if any(not root.is_absolute() for root in project_roots.values()):
        raise ValidationError("Claude target project roots must be absolute")
    if len(set(project_roots.values())) != len(project_roots):
        raise ValidationError("Claude session projects must map to distinct target roots")
    if not source_slugs:
        return manifest, dict(files)
    path_map = {
        path: path.replace(
            f"claude/projects/{slug}/",
            f"claude/projects/{_claude_project_slug(project_roots[slug])}/",
            1,
        )
        for path in claude_paths
        for slug in (path.split("/", 3)[2],)
    }
    mapped_files: dict[str, bytes] = {}
    mapped_entries: list[SyncFile] = []
    for entry in manifest.files:
        mapped_path = path_map.get(entry.path, entry.path)
        content = files[entry.path]
        if entry.path in path_map and entry.path.endswith(".jsonl"):
            lines = []
            for raw_line in content.splitlines(keepends=True):
                try:
                    record = json.loads(raw_line)
                except json.JSONDecodeError:
                    lines.append(raw_line)
                    continue
                if isinstance(record, dict) and isinstance(record.get("cwd"), str):
                    source_slug = entry.path.split("/", 3)[2]
                    record["cwd"] = str(project_roots[source_slug])
                    newline = b"\n" if raw_line.endswith(b"\n") else b""
                    raw_line = json.dumps(record, separators=(",", ":")).encode() + newline
                lines.append(raw_line)
            content = b"".join(lines)
        mapped_files[mapped_path] = content
        mapped_entries.append(
            replace(
                entry,
                path=mapped_path,
                size=len(content),
                sha256=hashlib.sha256(content).hexdigest(),
            )
        )
    return replace(manifest, files=tuple(mapped_entries)), mapped_files


def select_claude_project(
    manifest: SyncManifest, files: Mapping[str, bytes], source_slug: str
) -> tuple[SyncManifest, dict[str, bytes]]:
    """Keep one Claude project while retaining all non-Claude session content."""
    prefix = f"claude/projects/{source_slug}/"
    selected_entries = tuple(
        entry
        for entry in manifest.files
        if not entry.path.startswith("claude/projects/") or entry.path.startswith(prefix)
    )
    if not any(entry.path.startswith(prefix) for entry in selected_entries):
        raise ValidationError(f"unknown Claude source project: {source_slug}")
    selected_paths = {entry.path for entry in selected_entries}
    return replace(manifest, files=selected_entries), {
        path: content for path, content in files.items() if path in selected_paths
    }


def select_claude_projects(
    manifest: SyncManifest, files: Mapping[str, bytes], source_slugs: Sequence[str]
) -> tuple[SyncManifest, dict[str, bytes]]:
    """Keep the explicitly selected Claude projects and all non-Claude content."""
    selected = set(source_slugs)
    entries = tuple(
        entry
        for entry in manifest.files
        if not entry.path.startswith("claude/projects/") or entry.path.split("/", 3)[2] in selected
    )
    paths = {entry.path for entry in entries}
    return replace(manifest, files=entries), {
        path: content for path, content in files.items() if path in paths
    }


def codex_session_projects(files: Mapping[str, bytes]) -> tuple[str, ...]:
    database = files.get("codex/state_5.sqlite")
    if database is None:
        return ()
    with tempfile.NamedTemporaryFile(suffix=".sqlite") as temporary:
        temporary.write(database)
        temporary.flush()
        connection = sqlite3.connect(temporary.name)
        try:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(threads)")}
            if "cwd" not in columns:
                return ()
            values = connection.execute("SELECT DISTINCT cwd FROM threads").fetchall()
        finally:
            connection.close()
    return tuple(sorted(value for (value,) in values if isinstance(value, str) and value))


def _codex_logical_path(rollout_path: str) -> Optional[str]:
    marker = "/.codex/sessions/"
    if marker in rollout_path:
        return "codex/sessions/" + rollout_path.split(marker, 1)[1]
    if rollout_path.startswith(".codex/sessions/"):
        return "codex/sessions/" + rollout_path.split(".codex/sessions/", 1)[1]
    return None


def select_codex_projects(
    manifest: SyncManifest, files: Mapping[str, bytes], source_projects: Sequence[str]
) -> tuple[SyncManifest, dict[str, bytes]]:
    """Keep only Codex threads and JSONL files for explicitly selected cwd values."""
    selected = set(source_projects)
    database_path = "codex/state_5.sqlite"
    if database_path not in files or not selected:
        entries = tuple(entry for entry in manifest.files if not entry.path.startswith("codex/"))
        paths = {entry.path for entry in entries}
        return replace(manifest, files=entries), {
            path: content for path, content in files.items() if path in paths
        }
    with tempfile.NamedTemporaryFile(suffix=".sqlite") as temporary:
        temporary.write(files[database_path])
        temporary.flush()
        connection = sqlite3.connect(temporary.name)
        try:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(threads)")}
            if not {"id", "rollout_path", "cwd"}.issubset(columns):
                raise ValidationError("Codex SQLite database cannot select projects")
            placeholders = ",".join("?" for _ in selected)
            selected_values = tuple(sorted(selected))
            rows = connection.execute(
                f"SELECT rollout_path FROM threads WHERE cwd IN ({placeholders})",
                selected_values,
            ).fetchall()
            keep_paths = {
                logical
                for (rollout_path,) in rows
                if isinstance(rollout_path, str)
                for logical in (_codex_logical_path(rollout_path),)
                if logical is not None
            }
            tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
            selected_ids = f"SELECT id FROM threads WHERE cwd IN ({placeholders})"
            if "thread_dynamic_tools" in tables:
                connection.execute(
                    f"DELETE FROM thread_dynamic_tools WHERE thread_id NOT IN ({selected_ids})",
                    selected_values,
                )
            if "thread_spawn_edges" in tables:
                connection.execute(
                    f"DELETE FROM thread_spawn_edges WHERE parent_thread_id NOT IN ({selected_ids}) "
                    f"OR child_thread_id NOT IN ({selected_ids})",
                    selected_values + selected_values,
                )
            connection.execute(
                f"DELETE FROM threads WHERE cwd NOT IN ({placeholders})", selected_values
            )
            connection.commit()
        finally:
            connection.close()
        temporary.seek(0)
        selected_database = temporary.read()
    selected_files = {
        path: content
        for path, content in files.items()
        if not path.startswith("codex/") or path == database_path or path in keep_paths
    }
    selected_files[database_path] = selected_database
    entries = tuple(
        replace(
            entry,
            size=len(selected_database),
            sha256=hashlib.sha256(selected_database).hexdigest(),
        )
        if entry.path == database_path
        else entry
        for entry in manifest.files
        if not entry.path.startswith("codex/")
        or entry.path == database_path
        or entry.path in keep_paths
    )
    return replace(manifest, files=entries), selected_files


def map_codex_sessions(
    manifest: SyncManifest,
    files: Mapping[str, bytes],
    target_home: Path,
    project_roots: Optional[Mapping[str, Path]] = None,
) -> tuple[SyncManifest, dict[str, bytes]]:
    """Rewrite Codex paths and project cwd values for the target machine."""
    if "sessions" not in manifest.selection:
        return manifest, dict(files)
    database_path = "codex/state_5.sqlite"
    if database_path not in files:
        return manifest, dict(files)
    if project_roots:
        if any(not root.is_absolute() for root in project_roots.values()):
            raise ValidationError("Codex target project roots must be absolute")
        if len(set(project_roots.values())) != len(project_roots):
            raise ValidationError("Codex session projects must map to distinct target roots")
    content = files[database_path]
    with tempfile.NamedTemporaryFile(suffix=".sqlite") as temporary:
        temporary.write(content)
        temporary.flush()
        connection = sqlite3.connect(temporary.name)
        try:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(threads)")}
            if "rollout_path" not in columns:
                return manifest, dict(files)
            has_cwd = "cwd" in columns
            query = (
                "SELECT id, rollout_path, cwd FROM threads"
                if has_cwd
                else "SELECT id, rollout_path, NULL FROM threads"
            )
            rows = connection.execute(query).fetchall()
            for thread_id, rollout_path, cwd in rows:
                if not isinstance(rollout_path, str):
                    continue
                logical = _codex_logical_path(rollout_path)
                if logical is None:
                    continue
                relative = logical.split("codex/sessions/", 1)[1]
                mapped = str(target_home / ".codex/sessions" / relative)
                mapped_cwd = (
                    project_roots.get(cwd, cwd) if project_roots and isinstance(cwd, str) else cwd
                )
                if has_cwd:
                    connection.execute(
                        "UPDATE threads SET rollout_path = ?, cwd = ? WHERE id = ?",
                        (mapped, str(mapped_cwd), thread_id),
                    )
                else:
                    connection.execute(
                        "UPDATE threads SET rollout_path = ? WHERE id = ?", (mapped, thread_id)
                    )
            connection.commit()
        finally:
            connection.close()
        temporary.seek(0)
        mapped_database = temporary.read()
    mapped_files = dict(files)
    mapped_files[database_path] = mapped_database
    if project_roots:
        for logical, original in list(mapped_files.items()):
            if not logical.startswith("codex/sessions/") or not logical.endswith(".jsonl"):
                continue
            lines = []
            for raw_line in original.splitlines(keepends=True):
                try:
                    record = json.loads(raw_line)
                except json.JSONDecodeError:
                    lines.append(raw_line)
                    continue
                changed = False
                if isinstance(record, dict) and isinstance(record.get("cwd"), str):
                    target = project_roots.get(record["cwd"])
                    if target is not None:
                        record["cwd"] = str(target)
                        changed = True
                if isinstance(record, dict) and isinstance(record.get("payload"), dict):
                    cwd = record["payload"].get("cwd")
                    if isinstance(cwd, str) and cwd in project_roots:
                        record["payload"]["cwd"] = str(project_roots[cwd])
                        changed = True
                if changed:
                    newline = b"\n" if raw_line.endswith(b"\n") else b""
                    raw_line = json.dumps(record, separators=(",", ":")).encode() + newline
                lines.append(raw_line)
            mapped_files[logical] = b"".join(lines)
    entries = tuple(
        replace(
            entry,
            size=len(mapped_files[entry.path]),
            sha256=hashlib.sha256(mapped_files[entry.path]).hexdigest(),
        )
        if entry.path in mapped_files and mapped_files[entry.path] != files[entry.path]
        else entry
        for entry in manifest.files
    )
    return replace(manifest, files=entries), mapped_files


def _hash_file(path: Path) -> tuple[int, str]:
    size = 0
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_FILE:
                raise ValidationError("sync file exceeds 256 MiB")
            digest.update(chunk)
    return size, digest.hexdigest()


def _derive_key(password: str, salt: bytes) -> bytes:
    try:
        return hashlib.scrypt(
            password.encode(), salt=salt, n=2**17, r=8, p=1, maxmem=SCRYPT_MAXMEM, dklen=32
        )
    except ValueError as exc:
        raise ValidationError("sync encryption parameters are unavailable on this host") from exc


def collect_files(
    home: Path,
    selections: Sequence[str],
    include_keys: bool = False,
    clients: Optional[Sequence[str]] = None,
) -> tuple[tuple[str, Path], ...]:
    selected = set(selections)
    if not selected or not selected.issubset(SELECTIONS):
        raise ValidationError("at least one supported sync selection is required")
    client_set = {"claude", "codex"} if clients is None else set(clients)
    if not client_set.issubset({"claude", "codex"}):
        raise ValidationError("unsupported sync client")
    roots: list[tuple[str, Path]] = []
    if "config" in selected:
        if "claude" in client_set:
            roots.append(("claude/settings.json", home / ".claude/settings.json"))
        if "codex" in client_set:
            roots.append(("codex/config.toml", home / ".codex/config.toml"))
        # Codex auth.json is included only for a registered/custom API-key profile.
        # Official ChatGPT/API-key credentials are intentionally never exported.
        auth_path = home / ".codex/auth.json"
        config_path = home / ".codex/config.toml"
        if "codex" in client_set and include_keys and auth_path.is_file() and config_path.is_file():
            try:
                auth = json.loads(auth_path.read_bytes())
                config = config_path.read_text()
            except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise ValidationError("Codex authentication files are invalid") from exc
            if auth.get("auth_mode") == "apikey" and 'model_provider = "openai"' not in config:
                roots.append(("codex/auth.json", auth_path))
    if "providers" in selected:
        roots.append(("codelux/providers.json", home / ".codelux/providers.json"))
    if "sessions" in selected:
        session_roots = []
        if "claude" in client_set:
            session_roots.append((home / ".claude/projects", "claude/projects"))
        if "codex" in client_set:
            session_roots.append((home / ".codex/sessions", "codex/sessions"))
        for base, prefix in session_roots:
            if base.exists():
                if base.is_symlink():
                    raise ValidationError(f"sync path is a symbolic link: {base}")
                for path in sorted(base.rglob("*")):
                    if path.is_symlink():
                        raise ValidationError(f"sync path is a symbolic link: {path}")
                    if path.is_dir():
                        continue
                    if path.is_file() and path.suffix == ".jsonl":
                        roots.append((f"{prefix}/{path.relative_to(base).as_posix()}", path))
                    # Session selections contain JSONL records only. Ignore
                    # ordinary metadata files such as macOS .DS_Store.
                    elif path.exists() and not path.is_file():
                        raise ValidationError(f"sync path is not an allowed regular file: {path}")
        db = home / ".codex/state_5.sqlite"
        if "codex" in client_set and db.exists():
            if db.is_symlink():
                raise ValidationError("Codex SQLite path is a symbolic link")
            roots.append(("codex/state_5.sqlite", db))
    result = []
    for logical, path in roots:
        _safe_relative(logical)
        if path.is_symlink():
            raise ValidationError(f"sync path is a symbolic link: {logical}")
        if path.is_file():
            if path.stat().st_nlink > 1 and logical in {
                "claude/.credentials.json",
                "codex/auth.json",
                "codelux/providers.json",
            }:
                raise ValidationError(f"sensitive sync file has multiple hard links: {logical}")
            result.append((logical, path))
    return tuple(result)


def build_manifest(
    home: Path,
    selections: Sequence[str],
    include_keys: bool = False,
    clients: Optional[Sequence[str]] = None,
) -> tuple[SyncManifest, tuple[tuple[str, Path], ...]]:
    if "providers" in selections and not include_keys:
        raise ValidationError("Provider synchronization requires credentials; omit --no-keys")
    files = collect_files(home, selections, include_keys, clients)
    if len(files) > MAX_FILES:
        raise ValidationError("sync file count exceeds limit")
    entries = []
    total = 0
    for logical, path in files:
        if logical == "codex/state_5.sqlite":
            content = _sqlite_backup_bytes(path)
            size, digest = len(content), hashlib.sha256(content).hexdigest()
        else:
            size, digest = _hash_file(path)
        total += size
        if total > MAX_TOTAL:
            raise ValidationError("sync content exceeds 4 GiB")
        entries.append(SyncFile(logical, path.stat().st_mode & 0o777, size, digest))
    manifest = SyncManifest(
        secrets.token_hex(16),
        datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        machine_id(home / ".codelux"),
        tuple(sorted(set(selections))),
        include_keys,
        tuple(entries),
    )
    return manifest, files


def _sqlite_backup_bytes(path: Path) -> bytes:
    last: Optional[Exception] = None
    for attempt in range(SQLITE_BACKUP_ATTEMPTS):
        with tempfile.NamedTemporaryFile(suffix=".sqlite") as temporary:
            try:
                source = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
                target = sqlite3.connect(temporary.name)
                try:
                    source.backup(target)
                    row = target.execute("PRAGMA integrity_check").fetchone()
                    columns = {item[1] for item in target.execute("PRAGMA table_info(threads)")}
                    if row is None or row[0] != "ok" or "model_provider" not in columns:
                        raise ValidationError("Codex SQLite database is invalid")
                finally:
                    target.close()
                    source.close()
                return Path(temporary.name).read_bytes()
            except sqlite3.OperationalError as exc:
                last = exc
                if "busy" not in str(exc).lower() and "locked" not in str(exc).lower():
                    break
                if attempt + 1 < SQLITE_BACKUP_ATTEMPTS:
                    time.sleep(SQLITE_BACKUP_BACKOFF_SECONDS * (attempt + 1))
    detail = f": {last}" if last is not None else ""
    raise ValidationError(f"Codex SQLite backup failed{detail}") from last


def _source_content(logical: str, path: Union[Path, bytes], includes_keys: bool) -> bytes:
    if isinstance(path, bytes):
        return path
    if logical == "codex/state_5.sqlite":
        return _sqlite_backup_bytes(path)
    return path.read_bytes()


def materialize_sync_files(
    manifest: SyncManifest, files: Iterable[tuple[str, Union[Path, bytes]]]
) -> tuple[SyncManifest, dict[str, bytes]]:
    """Capture one stable byte snapshot and align manifest hashes with it."""
    payload = {
        logical: _source_content(logical, source, manifest.includes_keys)
        for logical, source in files
    }
    entries = tuple(
        replace(
            entry,
            size=len(payload[entry.path]),
            sha256=hashlib.sha256(payload[entry.path]).hexdigest(),
        )
        for entry in manifest.files
    )
    return replace(manifest, files=entries), payload


def export_encrypted(
    manifest: SyncManifest, files: Iterable[tuple[str, Path]], password: str, output: Path
) -> None:
    if len(password) < 12:
        raise ValidationError("sync password must contain at least 12 characters")
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as exc:
        raise ValidationError(
            "sync encryption dependency is unavailable; reinstall codelux"
        ) from exc
    output = output.absolute()
    if output.exists() or output.is_symlink():
        raise ValidationError("sync output already exists")
    payload = create_plain_archive(manifest, files)
    salt = secrets.token_bytes(32)
    nonce = secrets.token_bytes(12)
    key = _derive_key(password, salt)
    header = json.dumps(
        {"kdf": "scrypt", "n": 2**17, "r": 8, "p": 1, "salt": salt.hex(), "nonce": nonce.hex()},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    ciphertext = AESGCM(key).encrypt(nonce, payload, header)
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    atomic_write_private(
        output,
        MAGIC + struct.pack(">HI", VERSION, len(header)) + header + ciphertext,
        output.parent,
    )


def create_plain_archive(
    manifest: SyncManifest, files: Iterable[tuple[str, Union[Path, bytes]]]
) -> bytes:
    with tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024) as plain:
        with tarfile.open(fileobj=plain, mode="w") as archive:
            raw_manifest = json.dumps(
                manifest.to_dict(), sort_keys=True, separators=(",", ":")
            ).encode()
            info = tarfile.TarInfo("manifest.json")
            info.size = len(raw_manifest)
            info.mode = 0o600
            archive.addfile(info, io.BytesIO(raw_manifest))
            for logical, path in sorted(files):
                content = _source_content(logical, path, manifest.includes_keys)
                info = tarfile.TarInfo(logical)
                info.size = len(content)
                info.mode = 0o600
                archive.addfile(info, io.BytesIO(content))
        plain.seek(0)
        return plain.read()


def import_encrypted(path: Path, password: str) -> tuple[SyncManifest, dict[str, bytes]]:
    """Decrypt and validate an archive without touching a target HOME."""
    if len(password) < 12:
        raise ValidationError("sync password must contain at least 12 characters")
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as exc:
        raise ValidationError(
            "sync encryption dependency is unavailable; reinstall codelux"
        ) from exc
    raw = path.read_bytes()
    if len(raw) < len(MAGIC) + 6 or raw[: len(MAGIC)] != MAGIC:
        raise ValidationError("sync archive magic is invalid")
    version, header_size = struct.unpack(">HI", raw[len(MAGIC) : len(MAGIC) + 6])
    if version != VERSION or header_size > 64 * 1024:
        raise ValidationError("sync archive version or header is invalid")
    start = len(MAGIC) + 6
    header_raw = raw[start : start + header_size]
    try:
        header = json.loads(header_raw)
        salt = bytes.fromhex(header["salt"])
        nonce = bytes.fromhex(header["nonce"])
        if (
            header["kdf"] != "scrypt"
            or header["n"] != 2**17
            or header["r"] != 8
            or header["p"] != 1
        ):
            raise ValueError
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValidationError("sync archive header is invalid") from exc
    try:
        key = _derive_key(password, salt)
        plaintext = AESGCM(key).decrypt(nonce, raw[start + header_size :], header_raw)
    except Exception as exc:
        raise ValidationError("sync archive authentication failed") from exc
    return parse_plain_archive(plaintext)


def parse_plain_archive(plaintext: bytes) -> tuple[SyncManifest, dict[str, bytes]]:
    try:
        return _parse_plain_archive(plaintext)
    except tarfile.TarError as exc:
        raise ValidationError("sync archive format is invalid") from exc


def _parse_plain_archive(plaintext: bytes) -> tuple[SyncManifest, dict[str, bytes]]:
    if len(plaintext) > MAX_TOTAL + (MAX_FILES + 1) * 1024:
        raise ValidationError("sync archive exceeds total size limit")
    files: dict[str, bytes] = {}
    actual_total = 0
    with tarfile.open(fileobj=io.BytesIO(plaintext), mode="r:") as archive:
        members = archive.getmembers()
        if not members or members[0].name != "manifest.json":
            raise ValidationError("manifest.json must be the first archive member")
        if len(members) > MAX_FILES + 1:
            raise ValidationError("sync archive file count exceeds limit")
        manifest_member = archive.extractfile(members[0])
        if manifest_member is None:
            raise ValidationError("sync manifest is unreadable")
        manifest_raw = manifest_member.read(MAX_FILE + 1)
        if len(manifest_raw) > MAX_FILE:
            raise ValidationError("sync manifest exceeds size limit")
        try:
            data = json.loads(manifest_raw)
            manifest = SyncManifest.from_dict(data)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValidationError("sync manifest is invalid") from exc
        expected = {entry.path: entry for entry in manifest.files}
        for member in members[1:]:
            _safe_relative(member.name)
            if not member.isfile() or member.name in files or member.name not in expected:
                raise ValidationError("sync archive contains an unexpected member")
            handle = archive.extractfile(member)
            if handle is None:
                raise ValidationError("sync archive member is unreadable")
            content = handle.read(MAX_FILE + 1)
            entry = expected[member.name]
            if len(content) > MAX_FILE or member.size != entry.size:
                raise ValidationError("sync archive member exceeds size limit")
            actual_total += len(content)
            if actual_total > MAX_TOTAL:
                raise ValidationError("sync archive exceeds total size limit")
            if len(content) != entry.size or hashlib.sha256(content).hexdigest() != entry.sha256:
                raise ValidationError("sync archive member hash mismatch")
            if member.name == "codex/state_5.sqlite":
                _validate_sqlite_bytes(content)
            files[member.name] = content
        if set(files) != set(expected):
            raise ValidationError("sync archive is missing a declared member")
    return manifest, files


def _validate_sqlite_bytes(content: bytes) -> None:
    import sqlite3

    temporary = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
    name = temporary.name
    try:
        temporary.close()
        Path(name).write_bytes(content)
        connection = sqlite3.connect(name)
        try:
            result = connection.execute("PRAGMA integrity_check").fetchone()
            if result is None or result[0] != "ok":
                raise ValidationError("Codex SQLite archive failed integrity_check")
            columns = {row[1] for row in connection.execute("PRAGMA table_info(threads)")}
            if "model_provider" not in columns:
                raise ValidationError("Codex SQLite archive has an incompatible threads table")
        finally:
            connection.close()
    except sqlite3.DatabaseError as exc:
        raise ValidationError("Codex SQLite archive is malformed") from exc
    finally:
        try:
            Path(name).unlink()
        except FileNotFoundError:
            pass


def _atomic_write_sqlite(target: Path, content: bytes, root: Path) -> None:
    """Replace a SQLite database only after validating it and discard stale journals."""
    _validate_sqlite_bytes(content)
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(target) + suffix)
        if sidecar.is_symlink() or (sidecar.exists() and not sidecar.is_file()):
            raise ValidationError("SQLite sidecar is unsafe")
    atomic_write_private(
        target, content, root, validator=lambda path: _validate_sqlite_bytes(path.read_bytes())
    )
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(target) + suffix)
        if sidecar.exists():
            sidecar.unlink()


def save_peer_baseline(
    root: Path, remote_id: str, manifest: SyncManifest, files: Mapping[str, bytes]
) -> None:
    state = load_sync_state(root)
    state["baselines"][_baseline_key(remote_id, manifest.selection)] = {
        "transfer_id": manifest.transfer_id,
        "files": {path: hashlib.sha256(content).hexdigest() for path, content in files.items()},
    }
    atomic_write_private(
        _state_path(root),
        (json.dumps(state, sort_keys=True, indent=2) + "\n").encode(),
        root,
    )


def _merge_provider_registry(target: Path, incoming: bytes, overwrite: bool) -> bytes:
    """Merge third-party Provider bindings without changing target current state."""
    if not target.is_file():
        return incoming
    try:
        current = json.loads(target.read_bytes())
        new = json.loads(incoming)
    except json.JSONDecodeError as exc:
        raise ValidationError("Provider Registry is invalid") from exc
    if not isinstance(current, dict) or not isinstance(new, dict):
        raise ValidationError("Provider Registry is invalid")
    current_providers = current.get("providers")
    new_providers = new.get("providers")
    if not isinstance(current_providers, dict) or not isinstance(new_providers, dict):
        raise ValidationError("Provider Registry is invalid")
    for name, provider in new_providers.items():
        if not isinstance(name, str) or not isinstance(provider, dict):
            raise ValidationError("Provider Registry is invalid")
        incoming_clients = provider.get("clients")
        if not isinstance(incoming_clients, dict):
            raise ValidationError("Provider Registry is invalid")
        existing = current_providers.get(name)
        if existing is None:
            current_providers[name] = provider
            continue
        if not isinstance(existing, dict) or not isinstance(existing.get("clients"), dict):
            raise ValidationError("Provider Registry is invalid")
        existing_clients = existing["clients"]
        for client, binding in incoming_clients.items():
            old_binding = existing_clients.get(client)
            if old_binding is None or old_binding == binding or overwrite:
                existing_clients[client] = binding
            else:
                raise ValidationError(
                    f"Provider binding conflict requires --overwrite: {name}/{client}"
                )
    # The target Registry owns current: importing Providers must never switch
    # the active client configuration.
    return (json.dumps(current, sort_keys=True, indent=2) + "\n").encode()


def apply_import(
    home: Path,
    manifest: SyncManifest,
    files: Mapping[str, bytes],
    overwrite: Union[bool, Mapping[str, bool]] = False,
    claude_project_root: Optional[Path] = None,
    codex_project_roots: Optional[Mapping[str, Path]] = None,
    operation_type: str = "sync_import",
) -> tuple[str, ...]:
    """Apply a fully validated archive as one compensating local transaction."""
    if operation_type not in {"sync_import", "sync_push", "sync_pull"}:
        raise ValidationError("sync operation type is invalid")
    if claude_project_root is not None:
        source_slugs = {
            entry.path.split("/", 3)[2]
            for entry in manifest.files
            if entry.path.startswith("claude/projects/")
        }
        if len(source_slugs) != 1:
            raise ValidationError("multiple Claude projects require explicit project mappings")
        manifest, files = map_claude_sessions(
            manifest, files, {next(iter(source_slugs)): claude_project_root.absolute()}
        )
    manifest, files = map_codex_sessions(manifest, files, home.absolute(), codex_project_roots)
    root = home / ".codelux"
    # machine-id is diagnostic metadata only; equal identities do not block a clone.
    baseline = load_sync_state(root)["baselines"].get(
        _baseline_key(manifest.source_machine_id, manifest.selection), {"files": {}}
    )
    baseline_files = baseline.get("files", {}) if isinstance(baseline, dict) else {}
    if not isinstance(baseline_files, dict):
        raise ValidationError("sync-state.json is invalid")
    prepared: list[_PreparedWrite] = []
    conflicts = []
    missing = []

    def allows(path: str) -> bool:
        if isinstance(overwrite, bool):
            return overwrite
        if path.startswith("claude/"):
            return bool(overwrite.get("claude"))
        if path.startswith("codex/"):
            return bool(overwrite.get("codex"))
        return bool(overwrite.get("providers"))

    for entry in manifest.files:
        target = _logical_target(home, entry.path)
        if target.is_symlink():
            raise ValidationError("sync target is a symbolic link")
        incoming = files[entry.path]
        before = target.read_bytes() if target.is_file() else None
        compare_before = before
        provider_merge = entry.path == "codelux/providers.json"
        if provider_merge:
            incoming = _merge_provider_registry(target, incoming, allows(entry.path))
        current_hash = (
            hashlib.sha256(compare_before).hexdigest() if compare_before is not None else None
        )
        incoming_hash = entry.sha256
        old_hash = baseline_files.get(entry.path)
        if before is None:
            if old_hash is not None:
                missing.append(entry.path)
        elif not provider_merge and current_hash != incoming_hash and current_hash != old_hash:
            conflicts.append(entry.path)
        prepared.append(
            _PreparedWrite(
                entry.path,
                target,
                incoming,
                before,
                target.stat().st_mode & 0o777 if before is not None else 0o600,
                entry.mode,
            )
        )
    unauthorized = [path for path in conflicts if not allows(path)]
    if unauthorized:
        raise ValidationError(f"sync conflicts require --overwrite: {', '.join(unauthorized)}")

    baseline_target = _state_path(root)
    baseline_before = baseline_target.read_bytes() if baseline_target.is_file() else None
    prepared.append(
        _PreparedWrite(
            "codelux/sync-state.json",
            baseline_target,
            _baseline_bytes(root, manifest),
            baseline_before,
            baseline_target.stat().st_mode & 0o777 if baseline_before is not None else 0o600,
            0o600,
        )
    )

    operation_id = uuid.uuid4().hex
    store = SnapshotStore(root)
    operation_dir = store.backups / operation_id
    try:
        ensure_private_dir(operation_dir)
        manifest_files = []
        for item in prepared:
            backup = operation_dir / item.logical_path
            ensure_private_dir(backup.parent)
            backup_content = item.before if item.before is not None else b""
            atomic_write_private(backup, backup_content, operation_dir)
            digest = hashlib.sha256(backup_content).hexdigest()
            manifest_files.append(
                ManifestFile(
                    item.logical_path,
                    str(backup.relative_to(root)),
                    digest,
                    digest,
                    item.source_mode,
                    0o600,
                    source_existed=item.before is not None,
                )
            )
        operation = Manifest(
            1,
            operation_id,
            datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            operation_type,
            f"sync:{manifest.transfer_id}",
            tuple(),
            {},
            {},
            tuple(manifest_files),
        )
        store.write_manifest(operation)
    except CodeluxError:
        raise
    except Exception as exc:
        raise ValidationError("sync import preparation failed; target was not modified") from exc
    modified: list[int] = []
    try:
        operation = store.set_operation_state(operation, OperationState.COMMITTING)
        for index, item in enumerate(prepared):
            if item.before == item.incoming:
                continue
            # Persist the recovery intent before touching the target. Restoring an
            # unchanged file is harmless; omitting a file changed before a later
            # chmod/hash failure is not.
            modified.append(index)
            operation = store.update_file_state(operation, item.logical_path, FileState.MODIFIED)
            if item.logical_path == "codex/state_5.sqlite":
                _atomic_write_sqlite(item.target, item.incoming, item.target.parent)
            else:
                atomic_write_private(item.target, item.incoming, item.target.parent)
            item.target.chmod(item.target_mode)
            if (
                hashlib.sha256(item.target.read_bytes()).hexdigest()
                != hashlib.sha256(item.incoming).hexdigest()
            ):
                raise ValidationError("sync target hash mismatch after write")
            if item.target.stat().st_mode & 0o777 != item.target_mode:
                raise ValidationError("sync target mode mismatch after write")
        operation = store.set_operation_state(operation, OperationState.COMMITTED)
    except Exception as exc:
        recovery_failed = False
        for index in reversed(modified):
            item = prepared[index]
            try:
                if item.before is None:
                    if item.target.exists():
                        item.target.unlink()
                else:
                    if item.logical_path == "codex/state_5.sqlite":
                        _atomic_write_sqlite(item.target, item.before, item.target.parent)
                    else:
                        atomic_write_private(item.target, item.before, item.target.parent)
                    item.target.chmod(item.source_mode)
            except Exception:
                recovery_failed = True
        if recovery_failed:
            operation = store.require_recovery(operation)
            raise ValidationError("sync failed and recovery is required") from exc
        operation = store.update_all_file_states(operation, FileState.ROLLED_BACK)
        store.set_operation_state(operation, OperationState.ROLLED_BACK)
        raise ValidationError("sync import failed; target was restored") from exc
    return tuple(missing)
