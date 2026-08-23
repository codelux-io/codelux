import json
from collections.abc import Mapping
from pathlib import Path

import pytest

from codelux.adapters.base import ClientAdapter
from codelux.coordinator import TransactionCoordinator
from codelux.errors import CodeluxError, RecoveryRequiredError
from codelux.models import ConfigFile, ConfigState, ObservedConfig, PreparedChange, ProcessState
from codelux.snapshots import SnapshotStore


class FakeAdapter(ClientAdapter):
    def __init__(
        self,
        name: str,
        fail_commit: bool = False,
        fail_rollback: bool = False,
        recovery_commit: bool = False,
    ) -> None:
        self.name = name
        self.fail_commit = fail_commit
        self.fail_rollback = fail_rollback
        self.recovery_commit = recovery_commit
        self.commits = 0
        self.rollbacks = 0

    def is_installed(self) -> bool:
        return True

    def is_running(self) -> ProcessState:
        return ProcessState.NOT_RUNNING

    def inspect(self) -> ObservedConfig:
        return ObservedConfig(ConfigState.OFFICIAL_LOGIN, None, None, None)

    def prepare_provider(self, binding: Mapping[str, object]) -> PreparedChange:
        raise NotImplementedError

    def prepare_snapshot_restore(self, manifest: Mapping[str, object]) -> PreparedChange:
        raise NotImplementedError

    def validate_files(self, files: tuple[ConfigFile, ...]) -> None:
        return None

    def commit(self, change: PreparedChange) -> None:
        self.commits += 1
        if self.recovery_commit:
            raise RecoveryRequiredError("internal rollback failed")
        if self.fail_commit:
            raise RuntimeError("commit failure")

    def rollback(self, change: PreparedChange) -> None:
        self.rollbacks += 1
        if self.fail_rollback:
            raise RuntimeError("rollback failure")


def _change(client: str) -> PreparedChange:
    file = ConfigFile(Path(client), b"before", 0o600)
    return PreparedChange(
        client,
        (file,),
        (file,),
        ObservedConfig(ConfigState.CUSTOM, client, None, None),
    )


def test_coordinator_compensates_in_reverse_order() -> None:
    first = FakeAdapter("claude")
    second = FakeAdapter("codex", fail_commit=True)
    with pytest.raises(CodeluxError):
        TransactionCoordinator({"claude": first, "codex": second}).commit_all(
            (_change("claude"), _change("codex"))
        )
    assert first.commits == 1 and first.rollbacks == 1


def test_coordinator_surfaces_recovery_required() -> None:
    first = FakeAdapter("claude", fail_rollback=True)
    second = FakeAdapter("codex", fail_commit=True)
    with pytest.raises(RecoveryRequiredError):
        TransactionCoordinator({"claude": first, "codex": second}).commit_all(
            (_change("claude"), _change("codex"))
        )


def test_coordinator_writes_recovery_record_on_rollback_failure(tmp_path: Path) -> None:
    first = FakeAdapter("claude", fail_rollback=True)
    second = FakeAdapter("codex", fail_commit=True)
    changes = (_change("claude"), _change("codex"))
    store = SnapshotStore(tmp_path / ".codelux")
    manifest = store.create(changes, "switch", "proxy", {"claude": None, "codex": None})

    with pytest.raises(RecoveryRequiredError):
        TransactionCoordinator({"claude": first, "codex": second}).commit_all(
            changes, manifest, store
        )
    assert (store.root / "recovery.json").is_file()


def test_coordinator_records_adapter_internal_recovery_without_rolling_it_back(
    tmp_path: Path,
) -> None:
    first = FakeAdapter("claude")
    second = FakeAdapter("codex", recovery_commit=True)
    changes = (_change("claude"), _change("codex"))
    store = SnapshotStore(tmp_path / ".codelux")
    manifest = store.create(changes, "switch", "proxy", {"claude": None, "codex": None})

    with pytest.raises(RecoveryRequiredError, match="internal rollback failed"):
        TransactionCoordinator({"claude": first, "codex": second}).commit_all(
            changes, manifest, store
        )

    assert first.rollbacks == 1
    assert second.rollbacks == 0
    recovery = json.loads((store.root / "recovery.json").read_text())
    assert recovery["files"] == ["codex/codex"]
