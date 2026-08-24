"""Manifest-backed snapshot storage."""

import hashlib
import json
import os
import uuid
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple

from codelux.errors import ValidationError
from codelux.models import (
    FileState,
    Manifest,
    ManifestFile,
    OperationState,
    PreparedChange,
)
from codelux.safe_files import atomic_write_private, ensure_private_dir


class SnapshotStore:
    def __init__(self, root: Path) -> None:
        self.root = root.absolute()
        self.backups = self.root / "backups"

    def create(
        self,
        changes: Tuple[PreparedChange, ...],
        operation_type: str,
        target_provider: str,
        registry_current: Mapping[str, Optional[str]],
    ) -> Manifest:
        ensure_private_dir(self.root)
        ensure_private_dir(self.backups)
        operation_id = uuid.uuid4().hex
        operation_dir = self.backups / operation_id
        ensure_private_dir(operation_dir)
        manifest_files = []
        before_states = {}
        clients = []
        for change in changes:
            clients.append(change.client)
            before_states[change.client] = change.detected.state
            client_dir = operation_dir / change.client
            ensure_private_dir(client_dir)
            snapshot_files = list(change.before)
            if change.session is not None:
                snapshot_files.extend(change.session.before)
            seen = set()
            for before in snapshot_files:
                logical = before.logical_path or f"{change.client}/{before.path.name}"
                if logical in seen:
                    continue
                seen.add(logical)
                relative_source = Path(logical)
                if relative_source.is_absolute() or relative_source.parts[0] != change.client:
                    raise ValidationError("snapshot logical path is invalid")
                backup = operation_dir / relative_source
                ensure_private_dir(backup.parent)
                atomic_write_private(backup, before.content, operation_dir)
                digest = hashlib.sha256(before.content).hexdigest()
                manifest_files.append(
                    ManifestFile(
                        logical,
                        str(backup.relative_to(self.root)),
                        digest,
                        digest,
                        before.mode,
                        0o600,
                    )
                )
        manifest = Manifest(
            1,
            operation_id,
            datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            operation_type,
            target_provider,
            tuple(clients),
            before_states,
            dict(registry_current),
            tuple(manifest_files),
        )
        self.write_manifest(manifest)
        return manifest

    def write_manifest(self, manifest: Manifest) -> None:
        operation_dir = self.backups / manifest.operation_id
        ensure_private_dir(operation_dir)
        payload = json.dumps(manifest.to_dict(), ensure_ascii=True, indent=2).encode() + b"\n"
        atomic_write_private(operation_dir / "manifest.json", payload, self.root)

    def read_manifest(self, operation_id: str) -> Manifest:
        path = self.backups / operation_id / "manifest.json"
        try:
            raw = json.loads(path.read_bytes())
            manifest = Manifest.from_dict(raw)
            self.validate_manifest(manifest)
            return manifest
        except (
            OSError,
            json.JSONDecodeError,
            ValueError,
            TypeError,
            AttributeError,
            KeyError,
        ) as exc:
            raise ValidationError("snapshot manifest is invalid") from exc

    def latest_incomplete(self) -> Optional[Manifest]:
        if not self.backups.is_dir() or self.backups.is_symlink():
            return None
        incomplete = []
        for operation_dir in self.backups.iterdir():
            if not operation_dir.is_dir() or operation_dir.is_symlink():
                continue
            try:
                manifest = self.read_manifest(operation_dir.name)
            except ValidationError:
                continue
            if manifest.state not in {OperationState.COMMITTED, OperationState.ROLLED_BACK}:
                incomplete.append(manifest)
        return max(incomplete, key=lambda item: item.created_at) if incomplete else None

    def require_recovery(self, manifest: Manifest) -> Manifest:
        updated = self.update_all_file_states(manifest, FileState.RECOVERY_REQUIRED)
        updated = self.set_operation_state(updated, OperationState.RECOVERY_REQUIRED)
        self.write_recovery(updated)
        return updated

    def validate_manifest(self, manifest: Manifest) -> None:
        operation_dir = self.backups / manifest.operation_id
        if operation_dir.is_symlink() or not operation_dir.is_dir():
            raise ValidationError("snapshot operation directory is missing or unsafe")
        for file in manifest.files:
            backup = self.root / file.backup_path
            expected = operation_dir / file.source_path
            if backup != expected:
                raise ValidationError("snapshot backup path does not match its operation")
            if backup.is_symlink() or not backup.is_file():
                raise ValidationError("snapshot backup is missing or unsafe")
            content = backup.read_bytes()
            if hashlib.sha256(content).hexdigest() != file.backup_sha256:
                raise ValidationError("snapshot backup hash mismatch")
            if backup.stat().st_mode & 0o777 != file.backup_mode:
                raise ValidationError("snapshot backup permissions changed")

    def update_client_state(
        self,
        manifest: Manifest,
        client: str,
        file_state: FileState,
        operation_state: OperationState,
    ) -> Manifest:
        updated_files = tuple(
            replace(file, state=file_state) if file.source_path.startswith(f"{client}/") else file
            for file in manifest.files
        )
        updated = replace(manifest, files=updated_files, state=operation_state)
        self.write_manifest(updated)
        return updated

    def update_file_state(
        self, manifest: Manifest, source_path: str, file_state: FileState
    ) -> Manifest:
        matched = False
        updated_files = []
        for file in manifest.files:
            if file.source_path == source_path:
                matched = True
                updated_files.append(replace(file, state=file_state))
            else:
                updated_files.append(file)
        if not matched:
            raise ValidationError("snapshot manifest file is missing")
        updated = replace(manifest, files=tuple(updated_files))
        self.write_manifest(updated)
        return updated

    def set_operation_state(self, manifest: Manifest, state: OperationState) -> Manifest:
        updated = replace(manifest, state=state)
        self.write_manifest(updated)
        return updated

    def write_recovery(self, manifest: Manifest) -> None:
        payload = {
            "operation_id": manifest.operation_id,
            "manifest": f"backups/{manifest.operation_id}/manifest.json",
            "state": OperationState.RECOVERY_REQUIRED.value,
            "files": [
                file.source_path
                for file in manifest.files
                if file.state is FileState.RECOVERY_REQUIRED
            ],
        }
        atomic_write_private(
            self.root / "recovery.json",
            (json.dumps(payload, ensure_ascii=True, indent=2) + "\n").encode(),
            self.root,
        )

    def recover(self, home: Path, operation_id: str) -> Manifest:
        recovery_path = self.root / "recovery.json"
        if recovery_path.is_symlink() or not recovery_path.is_file():
            raise ValidationError("no recovery is required")
        try:
            recovery = json.loads(recovery_path.read_bytes())
        except (OSError, json.JSONDecodeError) as exc:
            raise ValidationError("recovery.json is invalid") from exc
        if recovery.get("operation_id") != operation_id:
            raise ValidationError("recovery operation does not match recovery.json")
        manifest = self.read_manifest(operation_id)
        recoverable = {FileState.MODIFIED, FileState.RECOVERY_REQUIRED}
        for file in reversed(manifest.files):
            if file.state not in recoverable:
                continue
            backup = self.root / file.backup_path
            if backup.is_symlink() or not backup.is_file():
                raise ValidationError("recovery backup is missing or unsafe")
            content = backup.read_bytes()
            if hashlib.sha256(content).hexdigest() != file.backup_sha256:
                raise ValidationError("recovery backup hash mismatch")
            target = _logical_target(home, file.source_path, manifest.target_roots)
            if not file.source_existed:
                if target.is_symlink():
                    raise ValidationError("recovery target is a symbolic link")
                if target.exists():
                    if not target.is_file():
                        raise ValidationError("recovery target is not a regular file")
                    target.unlink()
                continue
            if target.is_symlink():
                raise ValidationError("recovery target is a symbolic link")
            from codelux.safe_files import atomic_write_private

            atomic_write_private(target, content, target.parent)
            target.chmod(file.source_mode)
            if hashlib.sha256(target.read_bytes()).hexdigest() != file.source_sha256:
                raise ValidationError("recovery target hash mismatch after write")
            if target.stat().st_mode & 0o777 != file.source_mode:
                raise ValidationError("recovery target permissions mismatch after write")
        updated = self.update_all_file_states(manifest, FileState.ROLLED_BACK)
        updated = self.set_operation_state(updated, OperationState.ROLLED_BACK)
        os.unlink(recovery_path)
        return updated

    def update_all_file_states(self, manifest: Manifest, state: FileState) -> Manifest:
        updated = replace(
            manifest,
            files=tuple(replace(file, state=state) for file in manifest.files),
        )
        self.write_manifest(updated)
        return updated


def _claude_project_slug(project_root: Path) -> str:
    resolved = project_root.expanduser().absolute()
    return "-" + "-".join(part for part in resolved.parts if part not in ("/", ""))


def _logical_target(
    home: Path, source_path: str, project_roots: Optional[Mapping[str, str]] = None
) -> Path:
    targets = {
        "claude/settings.json": home / ".claude" / "settings.json",
        "codex/config.toml": home / ".codex" / "config.toml",
        "codex/auth.json": home / ".codex" / "auth.json",
        "codex/state_5.sqlite": home / ".codex" / "state_5.sqlite",
        "codelux/providers.json": home / ".codelux" / "providers.json",
        "codelux/sync-state.json": home / ".codelux" / "sync-state.json",
    }
    if source_path in targets:
        return targets[source_path]
    if source_path == "project-local-mcp.json":
        return home / ".claude.json"
    if source_path.startswith("codex/sessions/"):
        relative = Path(source_path.removeprefix("codex/"))
        if relative.is_absolute() or ".." in relative.parts:
            raise ValidationError("recovery session path escapes Codex root")
        target = home / ".codex" / relative
        try:
            target.relative_to(home / ".codex")
        except ValueError as exc:
            raise ValidationError("recovery session path escapes Codex root") from exc
        return target
    if source_path.startswith("claude/projects/"):
        relative = Path(source_path.removeprefix("claude/"))
        if relative.is_absolute() or ".." in relative.parts:
            raise ValidationError("recovery session path escapes Claude root")
        target = home / ".claude" / relative
        try:
            target.relative_to(home / ".claude")
        except ValueError as exc:
            raise ValidationError("recovery session path escapes Claude root") from exc
        return target
    if source_path.startswith("project-env/"):
        parts = Path(source_path).parts
        if len(parts) < 3 or project_roots is None or parts[1] not in project_roots:
            raise ValidationError("project environment mapping is missing")
        root = Path(project_roots[parts[1]])
        if not root.is_absolute() or not root.is_dir() or root.is_symlink():
            raise ValidationError("project environment target is missing or unsafe")
        relative = Path(*parts[2:])
        if relative.is_absolute() or ".." in relative.parts:
            raise ValidationError("project environment path escapes target root")
        target = root / relative
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise ValidationError("project environment path escapes target root") from exc
        return target
    if source_path.startswith("project-memory/"):
        parts = Path(source_path).parts
        if len(parts) < 3 or project_roots is None or parts[1] not in project_roots:
            raise ValidationError("project memory mapping is missing")
        root = Path(project_roots[parts[1]])
        if not root.is_absolute() or not root.is_dir() or root.is_symlink():
            raise ValidationError("project memory target is missing or unsafe")
        relative = Path(*parts[2:])
        if relative.is_absolute() or ".." in relative.parts:
            raise ValidationError("project memory path escapes target root")
        memory_root = home / ".claude" / "projects" / _claude_project_slug(root) / "memory"
        return memory_root / relative
    user_targets = {
        "user-env/codex/AGENTS.md": home / ".codex" / "AGENTS.md",
        "user-env/codex/AGENTS.override.md": home / ".codex" / "AGENTS.override.md",
        "user-env/codex/config.toml": home / ".codex" / "config.toml",
        "user-env/claude/CLAUDE.md": home / ".claude" / "CLAUDE.md",
        "user-env/claude/settings.json": home / ".claude" / "settings.json",
    }
    if source_path in user_targets:
        return user_targets[source_path]
    if source_path.startswith("user-env/codex/") and source_path.endswith(".config.toml"):
        return home / ".codex" / Path(source_path).name
    user_prefixes = {
        "user-env/agents/skills/": home / ".agents" / "skills",
        "user-env/claude/rules/": home / ".claude" / "rules",
        "user-env/claude/skills/": home / ".claude" / "skills",
        "user-env/claude/agents/": home / ".claude" / "agents",
        "user-env/claude/commands/": home / ".claude" / "commands",
        "user-env/claude/output-styles/": home / ".claude" / "output-styles",
        "user-env/codex/rules/": home / ".codex" / "rules",
        "user-env/codex/hooks/": home / ".codex" / "hooks",
    }
    for prefix, root in user_prefixes.items():
        if source_path.startswith(prefix):
            relative = Path(source_path.removeprefix(prefix))
            if not relative.parts or relative.is_absolute() or ".." in relative.parts:
                raise ValidationError("user environment path escapes target root")
            return root / relative
    raise ValidationError("recovery manifest contains an unknown source path")
