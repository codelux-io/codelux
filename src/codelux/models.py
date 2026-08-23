"""Foundational domain models for Provider management.

These models deliberately contain no filesystem or client-specific behavior.
Adapters and the transaction coordinator build on these stable contracts.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


class ConfigState(str, Enum):
    OFFICIAL_LOGIN = "official_login"
    OFFICIAL_API_KEY = "official_api_key"
    CUSTOM = "custom"
    EXTERNAL_OVERRIDE = "external_override"
    UNKNOWN = "unknown"


class HealthState(str, Enum):
    HEALTHY = "healthy"
    DRIFTED = "drifted"
    RECOVERY_REQUIRED = "recovery_required"


class ProcessState(str, Enum):
    NOT_RUNNING = "not_running"
    RUNNING = "running"
    UNKNOWN = "unknown"


class FileState(str, Enum):
    STAGED = "staged"
    MODIFIED = "modified"
    ROLLED_BACK = "rolled_back"
    RECOVERY_REQUIRED = "recovery_required"


class OperationState(str, Enum):
    PREPARED = "prepared"
    COMMITTING = "committing"
    COMMITTED = "committed"
    ROLLING_BACK = "rolling_back"
    ROLLED_BACK = "rolled_back"
    RECOVERY_REQUIRED = "recovery_required"


@dataclass(frozen=True)
class ObservedConfig:
    state: ConfigState
    provider_id: Optional[str]
    base_url: Optional[str]
    fingerprint: Optional[str]
    reasons: Tuple[str, ...] = ()


@dataclass(frozen=True)
class ConfigFile:
    path: Path
    content: bytes
    mode: int
    logical_path: Optional[str] = None


@dataclass(frozen=True)
class SessionChange:
    before: Tuple[ConfigFile, ...]
    after: Tuple[ConfigFile, ...]
    cleanup_paths: Tuple[Path, ...] = ()


@dataclass(frozen=True)
class PreparedChange:
    client: str
    before: Tuple[ConfigFile, ...]
    after: Tuple[ConfigFile, ...]
    detected: ObservedConfig
    session: Optional[SessionChange] = None


@dataclass(frozen=True)
class ManifestFile:
    source_path: str
    backup_path: str
    source_sha256: str
    backup_sha256: str
    source_mode: int
    backup_mode: int
    state: FileState = FileState.STAGED
    source_existed: bool = True

    def __post_init__(self) -> None:
        for digest in (self.source_sha256, self.backup_sha256):
            if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                raise ValueError("manifest hashes must be lowercase SHA256 values")
        source = Path(self.source_path)
        backup = Path(self.backup_path)
        if (
            source.is_absolute()
            or backup.is_absolute()
            or ".." in source.parts
            or ".." in backup.parts
        ):
            raise ValueError("manifest paths must be relative")
        for mode in (self.source_mode, self.backup_mode):
            if mode & 0o777 != mode:
                raise ValueError("manifest mode must contain permission bits only")


@dataclass(frozen=True)
class Manifest:
    schema_version: int
    operation_id: str
    created_at: str
    operation_type: str
    target_provider: str
    clients: Tuple[str, ...]
    before_states: Mapping[str, ConfigState]
    registry_current: Mapping[str, Optional[str]]
    files: Tuple[ManifestFile, ...]
    state: OperationState = OperationState.PREPARED

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("unsupported manifest schema_version")
        try:
            parsed = datetime.fromisoformat(self.created_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("manifest created_at must be an ISO-8601 timestamp") from exc
        if parsed.tzinfo is None:
            raise ValueError("manifest created_at must include a timezone")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "operation_id": self.operation_id,
            "created_at": self.created_at,
            "operation_type": self.operation_type,
            "target_provider": self.target_provider,
            "clients": list(self.clients),
            "before_states": {k: v.value for k, v in self.before_states.items()},
            "registry_current": dict(self.registry_current),
            "state": self.state.value,
            "files": [
                {
                    "source_path": f.source_path,
                    "backup_path": f.backup_path,
                    "source_sha256": f.source_sha256,
                    "backup_sha256": f.backup_sha256,
                    "source_mode": f.source_mode,
                    "backup_mode": f.backup_mode,
                    "state": f.state.value,
                    "source_existed": f.source_existed,
                }
                for f in self.files
            ],
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Manifest":
        required = {
            "schema_version",
            "operation_id",
            "created_at",
            "operation_type",
            "target_provider",
            "clients",
            "before_states",
            "registry_current",
            "state",
            "files",
        }
        missing = required.difference(raw)
        if missing:
            raise ValueError("manifest missing fields: " + ", ".join(sorted(missing)))
        files = tuple(
            ManifestFile(
                source_path=item["source_path"],
                backup_path=item["backup_path"],
                source_sha256=item["source_sha256"],
                backup_sha256=item["backup_sha256"],
                source_mode=int(item["source_mode"]),
                backup_mode=int(item["backup_mode"]),
                state=FileState(item["state"]),
                source_existed=bool(item.get("source_existed", True)),
            )
            for item in raw["files"]
        )
        return cls(
            schema_version=int(raw["schema_version"]),
            operation_id=str(raw["operation_id"]),
            created_at=str(raw["created_at"]),
            operation_type=str(raw["operation_type"]),
            target_provider=str(raw["target_provider"]),
            clients=tuple(str(c) for c in raw["clients"]),
            before_states={k: ConfigState(v) for k, v in raw["before_states"].items()},
            registry_current=dict(raw["registry_current"]),
            files=files,
            state=OperationState(raw["state"]),
        )
