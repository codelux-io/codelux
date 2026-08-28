import json
from dataclasses import replace
from pathlib import Path

import pytest

from codelux.errors import ValidationError
from codelux.models import (
    ConfigFile,
    ConfigState,
    FileState,
    ObservedConfig,
    OperationState,
    PreparedChange,
)
from codelux.snapshots import SnapshotStore, _logical_target


def test_snapshot_store_rejects_unknown_storage_directory(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="backup directory is invalid"):
        SnapshotStore(tmp_path / ".codelux", "session-history")


def test_snapshot_store_writes_manifest_and_backup(tmp_path: Path) -> None:
    source = tmp_path / ".claude" / "settings.json"
    source.parent.mkdir()
    before = ConfigFile(source, b'{"env": {}}\n', 0o600)
    after = ConfigFile(source, b'{"env": {"x": "y"}}\n', 0o600)
    change = PreparedChange(
        "claude",
        (before,),
        (after,),
        ObservedConfig(ConfigState.OFFICIAL_LOGIN, None, None, None),
    )
    store = SnapshotStore(tmp_path / ".codelux")
    manifest = store.create((change,), "switch", "proxy", {"claude": None})

    restored = store.read_manifest(manifest.operation_id)
    backup = store.root / restored.files[0].backup_path
    assert backup.read_bytes() == before.content
    assert restored.before_states["claude"] is ConfigState.OFFICIAL_LOGIN


def test_recovery_hash_mismatch_preserves_recovery_record(tmp_path: Path) -> None:
    source = tmp_path / ".claude" / "settings.json"
    source.parent.mkdir()
    source.write_bytes(b"live")
    before = ConfigFile(source, b"official", 0o600)
    change = PreparedChange(
        "claude",
        (before,),
        (ConfigFile(source, b"custom", 0o600),),
        ObservedConfig(ConfigState.OFFICIAL_LOGIN, None, None, None),
    )
    store = SnapshotStore(tmp_path / ".codelux")
    manifest = store.create((change,), "switch", "proxy", {"claude": None})
    manifest = store.update_client_state(
        manifest,
        "claude",
        FileState.RECOVERY_REQUIRED,
        OperationState.RECOVERY_REQUIRED,
    )
    store.write_recovery(manifest)
    (store.root / manifest.files[0].backup_path).write_bytes(b"tampered")

    with pytest.raises(ValidationError, match="hash mismatch"):
        store.recover(tmp_path, manifest.operation_id)
    assert (store.root / "recovery.json").is_file()
    assert source.read_bytes() == b"live"


def test_recovery_skips_files_already_rolled_back(tmp_path: Path) -> None:
    source = tmp_path / ".claude" / "settings.json"
    source.parent.mkdir()
    before = ConfigFile(source, b"official", 0o600)
    change = PreparedChange(
        "claude",
        (before,),
        (ConfigFile(source, b"custom", 0o600),),
        ObservedConfig(ConfigState.OFFICIAL_LOGIN, None, None, None),
    )
    store = SnapshotStore(tmp_path / ".codelux")
    manifest = store.create((change,), "switch", "proxy", {"claude": None})
    manifest = store.update_client_state(
        manifest, "claude", FileState.ROLLED_BACK, OperationState.RECOVERY_REQUIRED
    )
    store.write_recovery(manifest)
    source.write_bytes(b"external-change")

    store.recover(tmp_path, manifest.operation_id)

    assert source.read_bytes() == b"external-change"


def test_recovery_record_validation_is_fail_closed(tmp_path: Path) -> None:
    store = SnapshotStore(tmp_path / ".codelux")
    with pytest.raises(ValidationError, match="no recovery"):
        store.recover(tmp_path, "missing")
    store.root.mkdir()
    recovery = store.root / "recovery.json"
    recovery.write_text("not-json")
    with pytest.raises(ValidationError, match="recovery.json is invalid"):
        store.recover(tmp_path, "missing")
    recovery.write_text(json.dumps({"operation_id": "other"}))
    with pytest.raises(ValidationError, match="does not match"):
        store.recover(tmp_path, "missing")


def test_snapshot_manifest_tampering_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / ".claude/settings.json"
    source.parent.mkdir()
    before = ConfigFile(source, b"official", 0o600)
    change = PreparedChange(
        "claude",
        (before,),
        (ConfigFile(source, b"custom", 0o600),),
        ObservedConfig(ConfigState.OFFICIAL_LOGIN, None, None, None),
    )
    store = SnapshotStore(tmp_path / ".codelux")
    manifest = store.create((change,), "switch", "proxy", {"claude": None})
    file = manifest.files[0]
    backup = store.root / file.backup_path

    backup.chmod(0o644)
    with pytest.raises(ValidationError, match="permissions changed"):
        store.validate_manifest(manifest)
    backup.chmod(0o600)
    wrong_path = replace(
        manifest,
        files=(replace(file, backup_path="backups/other/claude/settings.json"),),
    )
    with pytest.raises(ValidationError, match="does not match"):
        store.validate_manifest(wrong_path)
    backup.unlink()
    with pytest.raises(ValidationError, match="missing or unsafe"):
        store.validate_manifest(manifest)
    (store.backups / manifest.operation_id / "manifest.json").write_text("not-json")
    with pytest.raises(ValidationError, match="manifest is invalid"):
        store.read_manifest(manifest.operation_id)


def test_manifest_metadata_read_does_not_hash_backup_payload(tmp_path: Path) -> None:
    source = tmp_path / ".claude/settings.json"
    source.parent.mkdir()
    change = PreparedChange(
        "claude",
        (ConfigFile(source, b"official", 0o600),),
        (ConfigFile(source, b"custom", 0o600),),
        ObservedConfig(ConfigState.OFFICIAL_LOGIN, None, None, None),
    )
    store = SnapshotStore(tmp_path / ".codelux")
    manifest = store.create((change,), "switch", "proxy", {"claude": None})
    backup = store.root / manifest.files[0].backup_path
    backup.write_bytes(b"tampered")

    assert store.read_manifest_metadata(manifest.operation_id).operation_id == manifest.operation_id
    with pytest.raises(ValidationError, match="hash mismatch"):
        store.read_manifest(manifest.operation_id)


def test_latest_incomplete_reads_metadata_without_validating_payload(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / ".claude/settings.json"
    source.parent.mkdir()
    change = PreparedChange(
        "claude",
        (ConfigFile(source, b"official", 0o600),),
        (ConfigFile(source, b"custom", 0o600),),
        ObservedConfig(ConfigState.OFFICIAL_LOGIN, None, None, None),
    )
    store = SnapshotStore(tmp_path / ".codelux")
    manifest = store.create((change,), "switch", "proxy", {"claude": None})
    monkeypatch.setattr(
        store,
        "validate_manifest",
        lambda manifest: (_ for _ in ()).throw(AssertionError("payload validation called")),
    )

    assert store.latest_incomplete() == manifest


def test_sync_transaction_store_prunes_finalized_payload(tmp_path: Path) -> None:
    source = tmp_path / ".claude/settings.json"
    source.parent.mkdir()
    change = PreparedChange(
        "claude",
        (ConfigFile(source, b"official", 0o600),),
        (ConfigFile(source, b"custom", 0o600),),
        ObservedConfig(ConfigState.OFFICIAL_LOGIN, None, None, None),
    )
    store = SnapshotStore(tmp_path / ".codelux", "sync-transactions")
    manifest = store.create((change,), "sync_import", "sync:test", {"claude": None})
    manifest = store.set_operation_state(manifest, OperationState.COMMITTED)

    store.prune_finalized()

    assert not (store.backups / manifest.operation_id).exists()


def test_update_file_state_rejects_unknown_manifest_path(tmp_path: Path) -> None:
    source = tmp_path / ".claude/settings.json"
    source.parent.mkdir()
    change = PreparedChange(
        "claude",
        (ConfigFile(source, b"official", 0o600),),
        (ConfigFile(source, b"custom", 0o600),),
        ObservedConfig(ConfigState.OFFICIAL_LOGIN, None, None, None),
    )
    store = SnapshotStore(tmp_path / ".codelux")
    manifest = store.create((change,), "switch", "proxy", {"claude": None})
    with pytest.raises(ValidationError, match="file is missing"):
        store.update_file_state(manifest, "codex/missing", FileState.MODIFIED)


def test_recovery_deletes_file_created_by_failed_operation(tmp_path: Path) -> None:
    target = tmp_path / ".claude/settings.json"
    target.parent.mkdir()
    change = PreparedChange(
        "claude",
        (ConfigFile(target, b"", 0o600),),
        (ConfigFile(target, b"new", 0o600),),
        ObservedConfig(ConfigState.OFFICIAL_LOGIN, None, None, None),
    )
    store = SnapshotStore(tmp_path / ".codelux")
    manifest = store.create((change,), "sync_import", "sync:test", {})
    manifest = replace(
        manifest,
        files=(
            replace(
                manifest.files[0],
                source_existed=False,
                state=FileState.RECOVERY_REQUIRED,
            ),
        ),
        state=OperationState.RECOVERY_REQUIRED,
    )
    store.write_manifest(manifest)
    store.write_recovery(manifest)
    target.write_bytes(b"new")

    store.recover(tmp_path, manifest.operation_id)

    assert not target.exists()
    assert not (store.root / "recovery.json").exists()


def test_recovery_logical_target_rejects_parent_segments_and_unknown_paths(
    tmp_path: Path,
) -> None:
    assert _logical_target(tmp_path, "codex/sessions/2026/session.jsonl") == (
        tmp_path / ".codex/sessions/2026/session.jsonl"
    )
    assert _logical_target(tmp_path, "claude/projects/project/session.jsonl") == (
        tmp_path / ".claude/projects/project/session.jsonl"
    )
    with pytest.raises(ValidationError, match="escapes Codex"):
        _logical_target(tmp_path, "codex/sessions/../../escape")
    with pytest.raises(ValidationError, match="escapes Claude"):
        _logical_target(tmp_path, "claude/projects/../../escape")
    with pytest.raises(ValidationError, match="unknown source path"):
        _logical_target(tmp_path, "other/file")
