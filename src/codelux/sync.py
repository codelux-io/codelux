"""Phase C synchronization primitives: safe collection, manifests and encryption."""

import hashlib
import io
import json
import os
import re
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

from codelux.client_paths import (
    claude_config_root as _claude_config_root,
    claude_project_memory_root as _claude_project_memory_root,
    claude_project_slug as _claude_project_slug,
)
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
SELECTIONS = {
    "config",
    "sessions",
    "providers",
    "project_env",
    "local_env",
    "user_env",
    "memory",
}
OVERWRITE_SCOPES = {"providers", "project_env", "user_env", "memory"}
SyncSource = Union[Path, bytes]
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
    project_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        payload = {
            "schema_version": self.schema_version,
            "transfer_id": self.transfer_id,
            "created_at": self.created_at,
            "source_machine_id": self.source_machine_id,
            "selection": list(self.selection),
            "includes_keys": self.includes_keys,
            "files": [item.to_dict() for item in self.files],
        }
        if self.schema_version == 2:
            payload["project_ids"] = list(self.project_ids)
        return payload

    @classmethod
    def from_dict(cls, data: Any) -> "SyncManifest":
        if not isinstance(data, dict):
            raise ValidationError("sync manifest is invalid")
        schema_version = data.get("schema_version")
        expected = {
            "schema_version",
            "transfer_id",
            "created_at",
            "source_machine_id",
            "selection",
            "includes_keys",
            "files",
        }
        if schema_version == 2:
            expected.add("project_ids")
        if set(data) != expected:
            raise ValidationError("sync manifest is invalid")
        if schema_version not in {1, 2} or not isinstance(data["includes_keys"], bool):
            raise ValidationError("sync manifest is invalid")
        raw_project_ids = data.get("project_ids", [])
        if (
            not isinstance(raw_project_ids, list)
            or any(not isinstance(item, str) for item in raw_project_ids)
            or tuple(raw_project_ids) != tuple(sorted(set(raw_project_ids)))
        ):
            raise ValidationError("sync manifest is invalid")
        project_ids = tuple(raw_project_ids)
        for project_id in project_ids:
            if re.fullmatch(r"p-[0-9a-f]{24}", project_id) is None:
                raise ValidationError("sync manifest is invalid")
        for field_name in ("transfer_id", "created_at", "source_machine_id"):
            if not isinstance(data[field_name], str) or not data[field_name]:
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
        if set(selection).intersection({"project_env", "local_env"}) and not project_ids:
            raise ValidationError("sync manifest is invalid")
        for item in files:
            if item.path.startswith(("project-env/", "project-memory/")):
                parts = PurePosixPath(item.path).parts
                if len(parts) < 3 or parts[1] not in project_ids:
                    raise ValidationError("sync manifest is invalid")
        return cls(
            data["transfer_id"],
            data["created_at"],
            data["source_machine_id"],
            tuple(selection),
            data["includes_keys"],
            tuple(files),
            schema_version,
            project_ids,
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


def _baseline_bytes(
    root: Path,
    manifest: SyncManifest,
    applied_hashes: Optional[Mapping[str, str]] = None,
) -> bytes:
    state = load_sync_state(root)
    hashes = applied_hashes or {}
    state["baselines"][_baseline_key(manifest.source_machine_id, manifest.selection)] = {
        "transfer_id": manifest.transfer_id,
        "files": {item.path: hashes.get(item.path, item.sha256) for item in manifest.files},
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


def session_project_candidates(home: Path) -> tuple[Path, ...]:
    """Return existing safe project roots referenced by local Claude and Codex history."""
    candidates: set[Path] = set()

    def add(value: object) -> None:
        if not isinstance(value, str):
            return
        path = Path(os.path.abspath(Path(value).expanduser()))
        if path == home or not path.is_dir() or path.is_symlink():
            return
        candidates.add(path)

    claude_root = _claude_config_root(home) / "projects"
    if claude_root.is_dir() and not claude_root.is_symlink():
        for path in sorted(claude_root.rglob("*.jsonl")):
            if path.is_symlink() or not path.is_file():
                continue
            try:
                with path.open("rb") as stream:
                    for raw_line in stream:
                        try:
                            record = json.loads(raw_line)
                        except (json.JSONDecodeError, UnicodeDecodeError):
                            continue
                        if isinstance(record, dict) and isinstance(record.get("cwd"), str):
                            add(record["cwd"])
                            break
            except OSError:
                continue

    database = home / ".codex" / "state_5.sqlite"
    if database.is_file() and not database.is_symlink():
        try:
            content = _sqlite_backup_bytes(database)
            for project in codex_session_projects({"codex/state_5.sqlite": content}):
                add(project)
        except (CodeluxError, OSError, sqlite3.Error):
            pass

    return tuple(sorted(candidates))


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


PROJECT_INSTRUCTION_NAMES = {"AGENTS.md", "AGENTS.override.md", "CLAUDE.md"}
PROJECT_SHARED_DIRS = {
    (".agents", "skills"),
    (".claude", "agent-memory"),
    (".claude", "hooks"),
    (".claude", "rules"),
    (".claude", "skills"),
    (".claude", "agents"),
    (".claude", "commands"),
    (".claude", "output-styles"),
    (".claude", "workflows"),
    (".codex", "agents"),
    (".codex", "rules"),
    (".codex", "hooks"),
}
USER_ENV_DIRS = {
    (".agents", "skills"),
    (".claude", "agent-memory"),
    (".claude", "hooks"),
    (".claude", "rules"),
    (".claude", "skills"),
    (".claude", "agents"),
    (".claude", "commands"),
    (".claude", "output-styles"),
    (".claude", "themes"),
    (".claude", "workflows"),
    (".codex", "agents"),
    (".codex", "rules"),
    (".codex", "hooks"),
    (".codex", "skills"),
}
WALK_PRUNE_NAMES = {
    ".git",
    ".hg",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".svn",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "target",
    "vendor",
}


def _project_id(project_root: Path) -> str:
    value = str(project_root.expanduser().absolute())
    return "p-" + hashlib.sha256(value.encode()).hexdigest()[:24]


def _project_metadata(project_roots: Sequence[Path]) -> dict[str, str]:
    projects: dict[str, str] = {}
    seen_roots = set()
    for value in project_roots:
        absolute = Path(os.path.abspath(value.expanduser()))
        if absolute.is_symlink():
            raise ValidationError(f"project root is missing or unsafe: {absolute}")
        try:
            resolved = absolute.resolve(strict=True)
        except OSError as exc:
            raise ValidationError(f"project root is missing or unsafe: {value}") from exc
        root = absolute
        if root in seen_roots:
            continue
        if not resolved.is_dir():
            raise ValidationError(f"project root is missing or unsafe: {root}")
        project_id = _project_id(absolute)
        if project_id in projects and projects[project_id] != str(root):
            raise ValidationError("project identity collision")
        projects[project_id] = str(root)
        seen_roots.add(root)
    return projects


def _codex_fallback_names(home: Path) -> set[str]:
    path = home / ".codex" / "config.toml"
    if not path.is_file() or path.is_symlink():
        return set()
    try:
        text = path.read_text()
    except (OSError, UnicodeDecodeError):
        return set()
    match = re.search(r"(?m)^\s*project_doc_fallback_filenames\s*=\s*\[(.*?)\]", text, re.S)
    if match is None:
        return set()
    names = set(re.findall(r"""["']([^"']+)["']""", match.group(1)))
    return {name for name in names if name and Path(name).name == name and "/" not in name}


def _walk_regular_files(root: Path, *, reject_symlinks: bool = True) -> Iterable[Path]:
    if not root.exists():
        return ()
    if root.is_symlink() or not root.is_dir():
        raise ValidationError(f"sync path is unsafe: {root}")
    result: list[Path] = []
    for current, directories, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        safe_directories = []
        for name in directories:
            candidate = current_path / name
            if name in WALK_PRUNE_NAMES:
                continue
            if candidate.is_symlink():
                continue
            safe_directories.append(name)
        directories[:] = safe_directories
        for name in files:
            path = current_path / name
            if path.is_symlink():
                if reject_symlinks:
                    raise ValidationError(f"sync path is a symbolic link: {path}")
                result.append(path)
                continue
            # Unix sockets, FIFOs, and device nodes can appear in otherwise valid
            # project trees. They are not portable content and are outside every
            # synchronization allowlist, so ignore them instead of aborting the
            # collection of regular files.
            if not path.is_file():
                continue
            result.append(path)
    return tuple(result)


def _project_file_allowed(relative: Path, local: bool, fallback_names: set[str]) -> bool:
    if relative.name == "CLAUDE.local.md":
        return local
    if relative.name in PROJECT_INSTRUCTION_NAMES or relative.name in fallback_names:
        return True
    if relative in {
        Path(".mcp.json"),
        Path(".worktreeinclude"),
        Path(".claude/settings.json"),
        Path(".codex/hooks.json"),
    }:
        return True
    if relative == Path(".claude/settings.local.json"):
        return local
    if relative == Path(".codex/config.toml"):
        return True
    return any(relative.parts[: len(prefix)] == prefix for prefix in PROJECT_SHARED_DIRS)


def _claude_imports(project_root: Path, seeds: Sequence[Path]) -> tuple[Path, ...]:
    try:
        resolved_root = project_root.resolve(strict=True)
    except OSError as exc:
        raise ValidationError("project root is missing or unsafe") from exc
    pending = []
    for seed in seeds:
        if seed.name != "CLAUDE.md":
            continue
        try:
            resolved_seed = seed.resolve(strict=True)
            resolved_seed.relative_to(resolved_root)
        except (OSError, RuntimeError, ValueError):
            continue
        pending.append((seed.absolute(), 0))
    found = set()
    while pending:
        path, depth = pending.pop()
        if depth >= 4:
            continue
        try:
            lines = path.read_text().splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        fenced = False
        for line in lines:
            if line.lstrip().startswith("```"):
                fenced = not fenced
                continue
            if fenced:
                continue
            without_inline_code = re.sub(r"`[^`]*`", "", line)
            for raw in re.findall(r"(?<!\w)@([^\s`]+)", without_inline_code):
                candidate_text = raw.rstrip(".,;:)]}")
                candidate = Path(candidate_text)
                if "~" in candidate_text or candidate.is_absolute():
                    continue
                logical_candidate = (path.parent / candidate).absolute()
                try:
                    resolved_candidate = logical_candidate.resolve(strict=True)
                    resolved_candidate.relative_to(resolved_root)
                except (OSError, RuntimeError, ValueError):
                    continue
                if not resolved_candidate.is_file():
                    continue
                if logical_candidate not in found:
                    found.add(logical_candidate)
                    pending.append((logical_candidate, depth + 1))
    return tuple(sorted(found))


def _collect_project_environment(
    home: Path, projects: Mapping[str, str], include_local: bool
) -> tuple[tuple[str, Path], ...]:
    fallback_names = _codex_fallback_names(home)
    result = []
    for project_id, source in projects.items():
        root = Path(source)
        selected = []
        for path in _walk_regular_files(root, reject_symlinks=False):
            relative = path.relative_to(root)
            if _project_file_allowed(relative, include_local, fallback_names):
                if path.is_symlink():
                    raise ValidationError(f"sync path is a symbolic link: {path}")
                selected.append(path)
        selected.extend(path for path in _claude_imports(root, selected) if path not in selected)
        for path in sorted(selected):
            relative_text = path.relative_to(root).as_posix()
            result.append((f"project-env/{project_id}/{relative_text}", path))
    return tuple(result)


def _collect_user_environment(home: Path) -> tuple[tuple[str, Path], ...]:
    roots = []
    claude_root = _claude_config_root(home)
    direct = {
        "user-env/codex/AGENTS.md": home / ".codex" / "AGENTS.md",
        "user-env/codex/AGENTS.override.md": home / ".codex" / "AGENTS.override.md",
        "user-env/codex/config.toml": home / ".codex" / "config.toml",
        "user-env/codex/hooks.json": home / ".codex" / "hooks.json",
        "user-env/claude/CLAUDE.md": claude_root / "CLAUDE.md",
        "user-env/claude/settings.json": claude_root / "settings.json",
        "user-env/claude/keybindings.json": claude_root / "keybindings.json",
    }
    for logical, path in direct.items():
        if path.is_symlink():
            raise ValidationError(f"sync path is a symbolic link: {path}")
        if path.is_file():
            roots.append((logical, path))
    codex_root = home / ".codex"
    if codex_root.is_dir() and not codex_root.is_symlink():
        for path in sorted(codex_root.glob("*.config.toml")):
            if path.is_symlink():
                raise ValidationError(f"sync path is a symbolic link: {path}")
            if path.is_file():
                roots.append((f"user-env/codex/{path.name}", path))
    for prefix in USER_ENV_DIRS:
        base = (
            claude_root.joinpath(*prefix[1:]) if prefix[0] == ".claude" else home.joinpath(*prefix)
        )
        for path in _walk_regular_files(base):
            if prefix == (".codex", "skills") and ".system" in path.relative_to(base).parts:
                continue
            logical_root = "user-env/" + "/".join(prefix).lstrip(".")
            roots.append((f"{logical_root}/{path.relative_to(base).as_posix()}", path))
    return tuple(roots)


def _collect_project_memory(
    home: Path, projects: Mapping[str, str]
) -> tuple[tuple[str, Path], ...]:
    result = []
    for project_id, source in projects.items():
        memory_root = _claude_project_memory_root(home, Path(source))
        for path in _walk_regular_files(memory_root):
            if path.suffix == ".md":
                result.append(
                    (
                        f"project-memory/{project_id}/{path.relative_to(memory_root).as_posix()}",
                        path,
                    )
                )
    codex_memory_root = home / ".codex" / "memories"
    for path in _walk_regular_files(codex_memory_root):
        result.append(
            (
                f"user-memory/codex/{path.relative_to(codex_memory_root).as_posix()}",
                path,
            )
        )
    return tuple(result)


def _claude_local_mcp_source(home: Path, projects: Mapping[str, str]) -> Optional[bytes]:
    path = home / ".claude.json"
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise ValidationError("Claude local project configuration is unsafe")
    try:
        data = json.loads(path.read_bytes())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError("Claude local project configuration is invalid") from exc
    if not isinstance(data, dict):
        raise ValidationError("Claude local project configuration is invalid")
    configured = data.get("projects", {})
    if not isinstance(configured, dict):
        raise ValidationError("Claude local project configuration is invalid")
    if not any(source in configured for source in projects.values()):
        return None
    return _claude_local_mcp_content(path, projects)


def collect_files(
    home: Path,
    selections: Sequence[str],
    include_keys: bool = False,
    clients: Optional[Sequence[str]] = None,
    project_roots: Sequence[Path] = (),
) -> tuple[tuple[str, SyncSource], ...]:
    selected = set(selections)
    if not selected or not selected.issubset(SELECTIONS):
        raise ValidationError("at least one supported sync selection is required")
    client_set = {"claude", "codex"} if clients is None else set(clients)
    if not client_set.issubset({"claude", "codex"}):
        raise ValidationError("unsupported sync client")
    projects = _project_metadata(project_roots)
    if selected.intersection({"project_env", "local_env"}) and not projects:
        raise ValidationError("project environment synchronization requires a project root")
    if "local_env" in selected and "project_env" not in selected:
        raise ValidationError("local project environment requires project environment selection")
    roots: list[tuple[str, SyncSource]] = []
    if "config" in selected:
        if "claude" in client_set:
            roots.append(("claude/settings.json", _claude_config_root(home) / "settings.json"))
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
    if "project_env" in selected:
        roots.extend(_collect_project_environment(home, projects, "local_env" in selected))
    if "local_env" in selected:
        local_mcp = _claude_local_mcp_source(home, projects)
        if local_mcp is not None:
            roots.append(("project-local-mcp.json", local_mcp))
    if "user_env" in selected:
        roots.extend(_collect_user_environment(home))
    if "memory" in selected:
        roots.extend(_collect_project_memory(home, projects))
    if "sessions" in selected:
        session_roots = []
        if "claude" in client_set:
            session_roots.append((_claude_config_root(home) / "projects", "claude/projects"))
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
    result: list[tuple[str, SyncSource]] = []
    seen_sources = set()
    for logical, source_item in roots:
        _safe_relative(logical)
        if isinstance(source_item, bytes):
            result.append((logical, source_item))
            continue
        path = source_item
        if path.is_symlink():
            raise ValidationError(f"sync path is a symbolic link: {logical}")
        if path.is_file():
            source = path.absolute()
            if source in seen_sources:
                continue
            if path.stat().st_nlink > 1 and logical in {
                "claude/.credentials.json",
                "codex/auth.json",
                "codelux/providers.json",
            }:
                raise ValidationError(f"sensitive sync file has multiple hard links: {logical}")
            result.append((logical, path))
            seen_sources.add(source)
    return tuple(result)


def build_manifest(
    home: Path,
    selections: Sequence[str],
    include_keys: bool = False,
    clients: Optional[Sequence[str]] = None,
    project_roots: Sequence[Path] = (),
) -> tuple[SyncManifest, tuple[tuple[str, SyncSource], ...]]:
    if "providers" in selections and not include_keys:
        raise ValidationError("Provider synchronization requires credentials; omit --no-keys")
    projects = _project_metadata(project_roots)
    files = collect_files(home, selections, include_keys, clients, project_roots)
    if len(files) > MAX_FILES:
        raise ValidationError("sync file count exceeds limit")
    entries = []
    total = 0
    for logical, path in files:
        if isinstance(path, bytes):
            size, digest = len(path), hashlib.sha256(path).hexdigest()
            mode = 0o600
        elif logical == "codex/state_5.sqlite":
            content = _sqlite_backup_bytes(path)
            size, digest = len(content), hashlib.sha256(content).hexdigest()
            mode = path.stat().st_mode & 0o777
        else:
            size, digest = _hash_file(path)
            mode = path.stat().st_mode & 0o777
        total += size
        if total > MAX_TOTAL:
            raise ValidationError("sync content exceeds 4 GiB")
        entries.append(SyncFile(logical, mode, size, digest))
    manifest = SyncManifest(
        secrets.token_hex(16),
        datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        machine_id(home / ".codelux"),
        tuple(sorted(set(selections))),
        include_keys,
        tuple(entries),
        2 if projects else 1,
        tuple(sorted(projects)),
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
    content = path.read_bytes()
    if logical.startswith("user-env/codex/") and logical.endswith(("config.toml", ".config.toml")):
        return _sanitize_codex_user_config(content)
    if (
        logical in {"user-env/claude/settings.json"}
        or logical.endswith("/.mcp.json")
        or logical.endswith("/.claude/settings.json")
        or logical.endswith("/.claude/settings.local.json")
        or logical == "user-env/codex/hooks.json"
        or logical.endswith("/.codex/hooks.json")
    ):
        return _sanitize_json_environment(
            content, strip_claude_routing=logical.startswith("user-env/")
        )
    return content


def _claude_local_mcp_content(path: Path, projects: Mapping[str, str]) -> bytes:
    try:
        data = json.loads(path.read_bytes())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError("Claude local project configuration is invalid") from exc
    if not isinstance(data, dict):
        raise ValidationError("Claude local project configuration is invalid")
    configured = data.get("projects", {})
    if not isinstance(configured, dict):
        raise ValidationError("Claude local project configuration is invalid")
    result = {}
    for project_id, source in projects.items():
        project = configured.get(source)
        if not isinstance(project, dict) or "mcpServers" not in project:
            continue
        servers = project["mcpServers"]
        if not isinstance(servers, dict):
            raise ValidationError("Claude local project MCP configuration is invalid")
        sanitized = json.loads(
            _sanitize_json_environment(json.dumps(servers).encode(), strip_claude_routing=False)
        )
        result[project_id] = sanitized
    return (json.dumps(result, sort_keys=True, indent=2) + "\n").encode()


def _sanitize_codex_user_config(content: bytes) -> bytes:
    try:
        lines = content.decode().splitlines(keepends=True)
    except UnicodeDecodeError as exc:
        raise ValidationError("Codex user configuration is invalid") from exc
    result = []
    skip_provider_table = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            skip_provider_table = stripped.startswith(
                ("[model_providers.", "[projects.", "[mcp_servers.")
            ) or stripped in {"[model_providers]", "[projects]", "[mcp_servers]"}
        if skip_provider_table:
            continue
        if re.match(r"^(model_provider|openai_base_url|chatgpt_base_url)\s*=", stripped):
            continue
        result.append(line)
    return "".join(result).encode()


def _sanitize_json_environment(content: bytes, *, strip_claude_routing: bool) -> bytes:
    try:
        data = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError("agent environment JSON is invalid") from exc

    def arguments_may_contain_secret(values: Sequence[Any]) -> bool:
        option = re.compile(
            r"(^|[^a-z0-9])(api[-_]?key|token|password|passwd|secret|authorization|cookie|credential)(=|$)",
            re.I,
        )
        secret_value = re.compile(r"^(sk[-_]|gh[pousr]_|github_pat_|xox[baprs]-|bearer\s+)", re.I)
        return any(
            isinstance(item, str) and (option.search(item) is not None or secret_value.search(item))
            for item in values
        )

    def clean(value: Any, parent: Optional[str] = None) -> Any:
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                normalized = str(key).upper()
                sensitive = any(
                    marker in normalized
                    for marker in (
                        "TOKEN",
                        "SECRET",
                        "PASSWORD",
                        "API_KEY",
                        "AUTHORIZATION",
                        "COOKIE",
                    )
                )
                routing = (
                    strip_claude_routing and parent == "env" and normalized.startswith("ANTHROPIC_")
                )
                if sensitive or routing:
                    continue
                result[key] = clean(item, str(key))
            return result
        if isinstance(value, list):
            if (
                parent is not None
                and parent.upper() in {"ARGS", "ARGUMENTS", "COMMAND_ARGS"}
                and arguments_may_contain_secret(value)
            ):
                return []
            return [clean(item, parent) for item in value]
        return value

    return (json.dumps(clean(data), sort_keys=True, indent=2) + "\n").encode()


def materialize_sync_files(
    manifest: SyncManifest, files: Iterable[tuple[str, SyncSource]]
) -> tuple[SyncManifest, dict[str, bytes]]:
    """Capture one stable byte snapshot and align manifest hashes with it."""
    payload = {}
    for logical, source in files:
        payload[logical] = _source_content(logical, source, manifest.includes_keys)
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
    manifest: SyncManifest, files: Iterable[tuple[str, SyncSource]], password: str, output: Path
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
    manifest, materialized = materialize_sync_files(manifest, files)
    payload = create_plain_archive(manifest, tuple(materialized.items()))
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


def create_plain_archive(manifest: SyncManifest, files: Iterable[tuple[str, SyncSource]]) -> bytes:
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
                    f"Provider binding conflict was not approved for overwrite: {name}/{client}"
                )
    # The target Registry owns current: importing Providers must never switch
    # the active client configuration.
    return (json.dumps(current, sort_keys=True, indent=2) + "\n").encode()


def _merge_claude_local_mcp(
    target: Path,
    incoming_bytes: bytes,
    target_roots: Mapping[str, str],
    overwrite: bool,
) -> bytes:
    try:
        incoming = json.loads(incoming_bytes)
        current = json.loads(target.read_bytes()) if target.is_file() else {}
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError("Claude local project configuration is invalid") from exc
    if not isinstance(incoming, dict) or not isinstance(current, dict):
        raise ValidationError("Claude local project configuration is invalid")
    current_projects = current.setdefault("projects", {})
    if not isinstance(current_projects, dict):
        raise ValidationError("Claude local project configuration is invalid")
    for project_id, incoming_servers in incoming.items():
        if project_id not in target_roots or not isinstance(incoming_servers, dict):
            raise ValidationError("Claude local project MCP mapping is invalid")
        target_root = target_roots[project_id]
        project = current_projects.setdefault(target_root, {})
        if not isinstance(project, dict):
            raise ValidationError("Claude local project configuration is invalid")
        current_servers = project.setdefault("mcpServers", {})
        if not isinstance(current_servers, dict):
            raise ValidationError("Claude local project configuration is invalid")
        for name, server in incoming_servers.items():
            existing = current_servers.get(name)
            if existing is None or existing == server or overwrite:
                current_servers[name] = server
            else:
                raise ValidationError(
                    f"Claude project MCP conflict was not approved for overwrite: "
                    f"{target_root}/{name}"
                )
    return (json.dumps(current, sort_keys=True, indent=2) + "\n").encode()


def apply_import(
    home: Path,
    manifest: SyncManifest,
    files: Mapping[str, bytes],
    overwrite: Union[bool, Mapping[str, bool]] = False,
    claude_project_root: Optional[Path] = None,
    codex_project_roots: Optional[Mapping[str, Path]] = None,
    environment_project_roots: Optional[Mapping[str, Path]] = None,
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
    referenced_projects = {
        entry.path.split("/", 2)[1]
        for entry in manifest.files
        if entry.path.startswith(("project-env/", "project-memory/"))
    }
    if any(entry.path == "project-local-mcp.json" for entry in manifest.files):
        referenced_projects.update(manifest.project_ids)
    supplied_projects = dict(environment_project_roots or {})
    if referenced_projects.difference(supplied_projects) or set(supplied_projects).difference(
        manifest.project_ids
    ):
        raise ValidationError("project environment mapping is incomplete")
    target_roots: dict[str, str] = {}
    for project_id, target_root in supplied_projects.items():
        target = target_root.expanduser().absolute()
        if project_id not in manifest.project_ids or not target.is_dir() or target.is_symlink():
            raise ValidationError("project environment target is missing or unsafe")
        if project_id in referenced_projects:
            target_roots[project_id] = str(target)
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
        if path == "codelux/providers.json":
            return bool(overwrite.get("providers"))
        if path.startswith("user-env/"):
            return bool(overwrite.get("user_env"))
        if path == "project-local-mcp.json":
            return bool(overwrite.get("project_env") or overwrite.get("claude"))
        if path.startswith("project-env/"):
            return bool(overwrite.get("project_env"))
        if path.startswith("project-memory/"):
            return bool(overwrite.get("memory"))
        if path.startswith("user-memory/"):
            return bool(overwrite.get("memory"))
        if path.startswith("claude/"):
            return bool(overwrite.get("claude"))
        if path.startswith("codex/"):
            return bool(overwrite.get("codex"))
        return bool(overwrite.get("providers"))

    for entry in manifest.files:
        target = _logical_target(home, entry.path, target_roots)
        if target.is_symlink():
            raise ValidationError("sync target is a symbolic link")
        incoming = files[entry.path]
        before = target.read_bytes() if target.is_file() else None
        compare_before = before
        provider_merge = entry.path == "codelux/providers.json"
        if provider_merge:
            incoming = _merge_provider_registry(target, incoming, allows(entry.path))
        local_mcp_merge = entry.path == "project-local-mcp.json"
        if local_mcp_merge:
            incoming = _merge_claude_local_mcp(target, incoming, target_roots, allows(entry.path))
        current_hash = (
            hashlib.sha256(compare_before).hexdigest() if compare_before is not None else None
        )
        incoming_hash = entry.sha256
        old_hash = baseline_files.get(entry.path)
        if before is None:
            if old_hash is not None:
                missing.append(entry.path)
        elif (
            not provider_merge
            and not local_mcp_merge
            and current_hash != incoming_hash
            and current_hash != old_hash
        ):
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
        raise ValidationError(
            "sync conflicts were not approved for overwrite: " + ", ".join(unauthorized)
        )

    baseline_target = _state_path(root)
    baseline_before = baseline_target.read_bytes() if baseline_target.is_file() else None
    applied_hashes = {
        item.logical_path: hashlib.sha256(item.incoming).hexdigest() for item in prepared
    }
    prepared.append(
        _PreparedWrite(
            "codelux/sync-state.json",
            baseline_target,
            _baseline_bytes(root, manifest, applied_hashes),
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
            target_roots=target_roots,
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
