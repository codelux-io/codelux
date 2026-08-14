import hashlib

import pytest

from codelux.models import (
    ConfigState,
    FileState,
    Manifest,
    ManifestFile,
    ObservedConfig,
    OperationState,
)


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def test_manifest_round_trip_preserves_states() -> None:
    manifest = Manifest(
        schema_version=1,
        operation_id="op-1",
        created_at="2026-08-07T12:00:00Z",
        operation_type="switch",
        target_provider="proxy",
        clients=("claude", "codex"),
        before_states={"claude": ConfigState.OFFICIAL_LOGIN},
        registry_current={"claude": "official", "codex": None},
        files=(
            ManifestFile(
                "claude/settings.json",
                "backup/settings.json",
                _digest(b"a"),
                _digest(b"a"),
                0o600,
                0o600,
            ),
        ),
        state=OperationState.ROLLED_BACK,
    )

    restored = Manifest.from_dict(manifest.to_dict())
    assert restored == manifest
    assert isinstance(restored.files[0].state, FileState)


def test_manifest_rejects_absolute_paths_and_bad_hashes() -> None:
    digest = _digest(b"a")
    with pytest.raises(ValueError, match="relative"):
        ManifestFile("/tmp/settings.json", "backup/settings.json", digest, digest, 0o600, 0o600)
    with pytest.raises(ValueError, match="relative"):
        ManifestFile(
            "codex/sessions/../../escape",
            "backup/../escape",
            digest,
            digest,
            0o600,
            0o600,
        )
    with pytest.raises(ValueError, match="SHA256"):
        ManifestFile("settings.json", "backup/settings.json", "bad", digest, 0o600, 0o600)


def test_observed_config_has_explicit_unknown_reasons() -> None:
    observed = ObservedConfig(ConfigState.UNKNOWN, None, None, None, ("invalid auth",))
    assert observed.state is ConfigState.UNKNOWN
    assert observed.reasons == ("invalid auth",)
