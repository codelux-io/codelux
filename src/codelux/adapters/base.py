"""Shared adapter interface."""

from abc import ABC, abstractmethod
from collections.abc import Mapping
from pathlib import Path

from codelux.models import ConfigFile, ObservedConfig, PreparedChange, ProcessState


class ClientAdapter(ABC):
    name: str

    @abstractmethod
    def is_installed(self) -> bool:
        """Return whether the client can be addressed on this machine."""

    @abstractmethod
    def is_running(self) -> ProcessState:
        """Return a conservative process state."""

    @abstractmethod
    def inspect(self) -> ObservedConfig:
        """Read actual client configuration without using registry cache."""

    @abstractmethod
    def prepare_provider(self, binding: Mapping[str, object]) -> PreparedChange:
        """Build a change without modifying live files."""

    @abstractmethod
    def prepare_snapshot_restore(self, manifest: Mapping[str, object]) -> PreparedChange:
        """Build a full-file restore from a validated snapshot manifest."""

    @abstractmethod
    def validate_files(self, files: tuple[ConfigFile, ...]) -> None:
        """Parse and validate candidate files."""

    @abstractmethod
    def commit(self, change: PreparedChange) -> None:
        """Commit a prepared change atomically for this client."""

    @abstractmethod
    def rollback(self, change: PreparedChange) -> None:
        """Restore the before files; repeated calls must be harmless."""


def config_file(path: Path, content: bytes, mode: int = 0o600) -> ConfigFile:
    return ConfigFile(path=path, content=content, mode=mode & 0o777)
