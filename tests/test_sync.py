import hashlib
import io
import json
import os
import sqlite3
import struct
import subprocess
import tarfile
from dataclasses import replace
from pathlib import Path

import pytest

import codelux.sync as sync_module
from codelux import sync_transport
from codelux.errors import ValidationError
from codelux.models import FileState, OperationState, ProcessState
from codelux.snapshots import SnapshotStore
from codelux.sync import (
    apply_import,
    build_manifest,
    create_plain_archive,
    export_encrypted,
    import_encrypted,
    load_sync_state,
    map_codex_sessions,
    materialize_sync_files,
    reset_baseline,
    rotate_machine_id,
    save_baseline,
    select_claude_projects,
    select_codex_projects,
    session_project_candidates,
)
from codelux.sync_transport import (
    Capability,
    canonical_line,
    discover_remote_project_candidates,
    local_capability,
    parse_plain_archive,
    pull_archive,
    push_archive,
    read_capability,
    read_path_payload,
    ssh_command,
    validate_capability,
)


def _home(tmp_path: Path) -> Path:
    (tmp_path / ".claude").mkdir(parents=True)
    (tmp_path / ".codex").mkdir(parents=True)
    (tmp_path / ".codelux").mkdir(parents=True)
    (tmp_path / ".claude/settings.json").write_text('{"keep":true}\n')
    (tmp_path / ".codex/config.toml").write_text('model_provider = "openai"\n')
    (tmp_path / ".codelux/providers.json").write_text(
        json.dumps({"schema_version": 1, "providers": {}, "current": {}})
    )
    return tmp_path


def _latest_operation(home: Path):
    store = SnapshotStore(home / ".codelux")
    operation_ids = [path.name for path in store.backups.iterdir() if path.is_dir()]
    assert len(operation_ids) == 1
    return store, store.read_manifest(operation_ids[0])


def _plain_tar(entries: list[tuple[str, bytes]]) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        for name, content in entries:
            info = tarfile.TarInfo(name)
            info.size = len(content)
            info.mode = 0o600
            archive.addfile(info, io.BytesIO(content))
    return output.getvalue()


def test_sync_manifest_schema_is_fail_closed(tmp_path: Path, monkeypatch) -> None:
    home = _home(tmp_path / "source")
    manifest, _ = build_manifest(home, ["config"])
    valid = manifest.to_dict()
    invalid_cases = []
    with pytest.raises(ValidationError, match="sync manifest is invalid"):
        sync_module.SyncManifest.from_dict(None)

    missing_key = dict(valid)
    missing_key.pop("transfer_id")
    invalid_cases.append(missing_key)
    invalid_cases.extend(
        [
            {**valid, "schema_version": 2},
            {**valid, "includes_keys": 1},
            {**valid, "transfer_id": ""},
            {**valid, "created_at": "not-a-date"},
            {**valid, "created_at": "2026-08-09T00:00:00"},
            {**valid, "selection": ["sessions", "config"]},
            {**valid, "selection": ["unknown"]},
            {**valid, "files": {}},
            {**valid, "files": ["not-a-file"]},
            {**valid, "files": [valid["files"][0], valid["files"][0]]},
            {
                **valid,
                "files": [{**valid["files"][0], "mode": True}],
            },
        ]
    )

    for raw in invalid_cases:
        with pytest.raises(ValidationError, match="sync manifest is invalid"):
            sync_module.SyncManifest.from_dict(raw)

    monkeypatch.setattr(sync_module, "MAX_TOTAL", 0)
    with pytest.raises(ValidationError, match="sync manifest is invalid"):
        sync_module.SyncManifest.from_dict(valid)


def test_sync_manifest_v2_project_metadata_is_strict(tmp_path: Path) -> None:
    home = _home(tmp_path / "home")
    project = tmp_path / "project"
    project.mkdir()
    (project / "AGENTS.md").write_text("rules\n")
    manifest, _ = build_manifest(home, ["project_env"], project_roots=(project,))
    raw = manifest.to_dict()
    assert sync_module.SyncManifest.from_dict(raw) == manifest
    assert str(project) not in json.dumps(raw)

    project_id = manifest.project_ids[0]
    foreign_file = dict(raw["files"][0])
    foreign_file["path"] = "project-env/p-ffffffffffffffffffffffff/AGENTS.md"
    invalid = [
        {**raw, "project_ids": {}},
        {**raw, "project_ids": ["bad"]},
        {**raw, "project_ids": [project_id, project_id]},
        {**raw, "project_ids": []},
        {**raw, "files": [foreign_file]},
    ]
    for value in invalid:
        with pytest.raises(ValidationError, match="sync manifest is invalid"):
            sync_module.SyncManifest.from_dict(value)


def test_encrypted_export_import_round_trip(tmp_path: Path) -> None:
    home = _home(tmp_path / "source")
    manifest, files = build_manifest(home, ["config", "providers"], include_keys=True)
    archive = tmp_path / "bundle.cdlx"
    export_encrypted(manifest, files, "correct horse battery", archive)
    restored, content = import_encrypted(archive, "correct horse battery")
    assert restored.transfer_id == manifest.transfer_id
    assert content["claude/settings.json"] == b'{"keep":true}\n'
    assert "api_key" not in archive.read_bytes().decode("latin1", "ignore")


def test_encrypted_import_rejects_wrong_password_without_target_write(tmp_path: Path) -> None:
    home = _home(tmp_path / "source")
    manifest, files = build_manifest(home, ["config"])
    archive = tmp_path / "bundle.cdlx"
    export_encrypted(manifest, files, "correct horse battery", archive)
    with pytest.raises(ValidationError, match="authentication"):
        import_encrypted(archive, "wrong password")


def test_encrypted_export_requires_strong_minimum_password(tmp_path: Path) -> None:
    home = _home(tmp_path / "source")
    manifest, files = build_manifest(home, ["config"])
    with pytest.raises(ValidationError, match="12"):
        export_encrypted(manifest, files, "too-short", tmp_path / "bundle.cdlx")


def test_encrypted_container_rejects_existing_output_and_invalid_inputs(
    tmp_path: Path, monkeypatch
) -> None:
    home = _home(tmp_path / "source")
    manifest, files = build_manifest(home, ["config"])
    existing = tmp_path / "existing.cdlx"
    existing.write_bytes(b"occupied")
    with pytest.raises(ValidationError, match="already exists"):
        export_encrypted(manifest, files, "correct horse battery", existing)
    with pytest.raises(ValidationError, match="12 characters"):
        import_encrypted(existing, "short")

    invalid_magic = tmp_path / "magic.cdlx"
    invalid_magic.write_bytes(b"not-a-codelux-archive")
    with pytest.raises(ValidationError, match="magic"):
        import_encrypted(invalid_magic, "correct horse battery")
    invalid_version = tmp_path / "version.cdlx"
    invalid_version.write_bytes(sync_module.MAGIC + struct.pack(">HI", 99, 0) + b"ciphertext")
    with pytest.raises(ValidationError, match="version or header"):
        import_encrypted(invalid_version, "correct horse battery")
    invalid_header = tmp_path / "header.cdlx"
    header = b'{"kdf":"wrong"}'
    invalid_header.write_bytes(
        sync_module.MAGIC + struct.pack(">HI", sync_module.VERSION, len(header)) + header
    )
    with pytest.raises(ValidationError, match="header is invalid"):
        import_encrypted(invalid_header, "correct horse battery")

    monkeypatch.setattr(
        sync_module.hashlib,
        "scrypt",
        lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("unsupported")),
    )
    with pytest.raises(ValidationError, match="parameters are unavailable"):
        sync_module._derive_key("correct horse battery", b"s" * 32)


def test_custom_codex_auth_is_included_but_official_auth_is_excluded(tmp_path: Path) -> None:
    home = _home(tmp_path / "source")
    auth = home / ".codex/auth.json"
    auth.write_text('{"auth_mode":"apikey","OPENAI_API_KEY":"third-party-key"}\n')
    (home / ".codex/config.toml").write_text('model_provider = "custom"\n')
    custom, _ = build_manifest(home, ["config"], include_keys=True)
    assert "codex/auth.json" in {item.path for item in custom.files}

    auth.write_text('{"auth_mode":"chatgpt","tokens":{"access_token":"secret"}}\n')
    (home / ".codex/config.toml").write_text('model_provider = "openai"\n')
    official, _ = build_manifest(home, ["config"], include_keys=True)
    assert "codex/auth.json" not in {item.path for item in official.files}
    assert "claude/.credentials.json" not in {item.path for item in official.files}


def test_no_keys_rejects_provider_registry_without_creating_a_partial_binding(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path / "source")
    registry = {
        "schema_version": 1,
        "providers": {
            "proxy": {
                "name": "proxy",
                "description": "",
                "clients": {
                    "codex": {
                        "enabled": True,
                        "base_url": "https://proxy.example",
                        "api_key": "must-not-cross-ssh",
                    }
                },
            }
        },
        "current": {"codex": "proxy"},
    }
    (home / ".codelux/providers.json").write_text(json.dumps(registry))
    with pytest.raises(ValidationError, match="requires credentials"):
        build_manifest(home, ["providers"], include_keys=False)


def test_sessions_ignore_regular_metadata_files(tmp_path: Path) -> None:
    home = _home(tmp_path / "source")
    projects = home / ".claude/projects/example"
    projects.mkdir(parents=True)
    (projects / ".DS_Store").write_bytes(b"metadata")
    (projects / "session.jsonl").write_text('{"type":"message"}\n')
    manifest, _ = build_manifest(home, ["sessions"])
    assert [item.path for item in manifest.files] == ["claude/projects/example/session.jsonl"]


def test_session_project_candidates_merge_existing_claude_and_codex_roots(tmp_path: Path) -> None:
    home = _home(tmp_path / "home")
    claude_project = tmp_path / "claude-project"
    codex_project = tmp_path / "codex-project"
    claude_project.mkdir()
    codex_project.mkdir()
    history = home / ".claude/projects/example"
    history.mkdir(parents=True)
    (history / "session.jsonl").write_text(
        "not-json\n" + json.dumps({"cwd": str(claude_project)}) + "\n"
    )
    with sqlite3.connect(home / ".codex/state_5.sqlite") as connection:
        connection.execute("CREATE TABLE threads (cwd TEXT, model_provider TEXT)")
        connection.executemany(
            "INSERT INTO threads VALUES (?, ?)",
            [
                (str(codex_project), "custom"),
                (str(claude_project), "custom"),
                (str(home), "custom"),
                (str(tmp_path / "missing"), "custom"),
            ],
        )

    assert session_project_candidates(home) == tuple(sorted((claude_project, codex_project)))


def test_project_environment_collects_agent_files_and_requires_local_opt_in(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path / "home")
    (home / ".codex/config.toml").write_text(
        'model_provider = "openai"\nproject_doc_fallback_filenames = ["TEAM_GUIDE.md"]\n'
    )
    project = tmp_path / "project"
    (project / ".claude/rules").mkdir(parents=True)
    (project / ".agents/skills/review").mkdir(parents=True)
    (project / ".codex").mkdir()
    (project / "src").mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("must not sync\n")
    (project / "linked").symlink_to(outside, target_is_directory=True)
    (project / "AGENTS.md").write_text("codex\n")
    (project / "CLAUDE.md").write_text(
        "Read @docs/workflow.md\nReject @linked/secret.md\nReject @~/secret.md\n"
    )
    (project / "CLAUDE.local.md").write_text("private\n")
    (project / "TEAM_GUIDE.md").write_text("fallback\n")
    (project / ".mcp.json").write_text("{}\n")
    (project / ".claude/settings.json").write_text("{}\n")
    (project / ".claude/settings.local.json").write_text("{}\n")
    (project / ".claude/rules/testing.md").write_text("tests\n")
    (project / ".agents/skills/review/SKILL.md").write_text("review\n")
    (project / ".codex/config.toml").write_text('sandbox_mode = "workspace-write"\n')
    (project / "src/AGENTS.override.md").write_text("nested\n")
    (project / "docs").mkdir()
    (project / "docs/workflow.md").write_text("workflow\n")
    (project / ".env").write_text("SECRET=no\n")

    shared, _ = build_manifest(home, ["project_env"], project_roots=(project,))
    shared_paths = {item.path.split("/", 2)[2] for item in shared.files}
    assert {
        "AGENTS.md",
        "CLAUDE.md",
        "TEAM_GUIDE.md",
        ".mcp.json",
        ".claude/settings.json",
        ".claude/rules/testing.md",
        ".agents/skills/review/SKILL.md",
        ".codex/config.toml",
        "src/AGENTS.override.md",
        "docs/workflow.md",
    }.issubset(shared_paths)
    assert "CLAUDE.local.md" not in shared_paths
    assert ".claude/settings.local.json" not in shared_paths
    assert ".env" not in shared_paths
    assert "linked/secret.md" not in shared_paths
    assert all("secret.md" not in path for path in shared_paths)

    local, _ = build_manifest(home, ["project_env", "local_env"], project_roots=(project,))
    local_paths = {item.path.split("/", 2)[2] for item in local.files}
    assert {"CLAUDE.local.md", ".claude/settings.local.json"}.issubset(local_paths)


def test_project_environment_ignores_non_regular_files(tmp_path: Path) -> None:
    home = _home(tmp_path / "home")
    project = tmp_path / "project"
    project.mkdir()
    (project / "AGENTS.md").write_text("portable instructions\n")
    os.mkfifo(project / "runtime.pipe")
    manifest, _ = build_manifest(home, ["project_env"], project_roots=(project,))

    assert [item.path.split("/", 2)[2] for item in manifest.files] == ["AGENTS.md"]


def test_user_environment_excludes_authentication_and_collects_extensions(tmp_path: Path) -> None:
    home = _home(tmp_path / "home")
    (home / ".codex/AGENTS.md").write_text("global\n")
    (home / ".codex/work.config.toml").write_text('model = "test"\n')
    (home / ".codex/auth.json").write_text('{"token":"never"}\n')
    (home / ".agents/skills/tool").mkdir(parents=True)
    (home / ".agents/skills/tool/SKILL.md").write_text("tool\n")
    (home / ".claude/agents").mkdir()
    (home / ".claude/agents/reviewer.md").write_text("review\n")
    (home / ".claude/.credentials.json").write_text('{"token":"never"}\n')
    (home / ".claude.json").write_text('{"oauth":"never"}\n')

    manifest, _ = build_manifest(home, ["user_env"])
    paths = {item.path for item in manifest.files}
    assert "user-env/codex/AGENTS.md" in paths
    assert "user-env/codex/work.config.toml" in paths
    assert "user-env/agents/skills/tool/SKILL.md" in paths
    assert "user-env/claude/agents/reviewer.md" in paths
    assert all("auth.json" not in path and "credentials" not in path for path in paths)

    manifest, sources = materialize_sync_files(manifest, _)
    target = _home(tmp_path / "target")
    apply_import(target, manifest, sources, overwrite=True)
    assert (target / ".codex/AGENTS.md").read_text() == "global\n"
    assert (target / ".codex/work.config.toml").read_text() == 'model = "test"\n'
    assert (target / ".agents/skills/tool/SKILL.md").read_text() == "tool\n"
    assert (target / ".claude/agents/reviewer.md").read_text() == "review\n"


def test_agent_environment_sanitizers_remove_routing_trust_and_secrets() -> None:
    codex = sync_module._sanitize_codex_user_config(
        b'model = "gpt"\nmodel_provider = "private"\n'
        b'[model_providers.private]\nbase_url = "https://secret"\n'
        b'[projects."/source"]\ntrust_level = "trusted"\n'
        b'[mcp_servers.private]\ncommand = "node"\n'
        b'[mcp_servers.private.env]\nAPI_KEY = "never"\n'
        b"[features]\nweb_search = true\n"
    )
    assert b'model = "gpt"' in codex
    assert b"model_provider" not in codex
    assert b"model_providers" not in codex
    assert b"trust_level" not in codex
    assert b"mcp_servers" not in codex
    assert b"API_KEY" not in codex
    assert b"web_search = true" in codex
    with pytest.raises(ValidationError, match="Codex user configuration"):
        sync_module._sanitize_codex_user_config(b"\xff")

    cleaned = json.loads(
        sync_module._sanitize_json_environment(
            json.dumps(
                {
                    "env": {
                        "ANTHROPIC_BASE_URL": "https://private",
                        "SAFE": "yes",
                        "PASSWORD": "never",
                    },
                    "items": [{"cookie": "never", "enabled": True}],
                    "args": ["server.js", "--api-key", "sk-secret"],
                }
            ).encode(),
            strip_claude_routing=True,
        )
    )
    assert cleaned == {
        "args": [],
        "env": {"SAFE": "yes"},
        "items": [{"enabled": True}],
    }
    with pytest.raises(ValidationError, match="environment JSON"):
        sync_module._sanitize_json_environment(b"not-json", strip_claude_routing=False)


def test_project_environment_and_memory_apply_to_mapped_target(tmp_path: Path) -> None:
    source_home = _home(tmp_path / "source-home")
    source_project = tmp_path / "source-project"
    source_project.mkdir()
    (source_project / "AGENTS.md").write_text("source rules\n")
    memory = (
        source_home
        / ".claude/projects"
        / sync_module._claude_project_slug(source_project)
        / "memory"
    )
    memory.mkdir(parents=True)
    (memory / "MEMORY.md").write_text("learned\n")
    manifest, sources = build_manifest(
        source_home, ["project_env", "memory"], project_roots=(source_project,)
    )
    manifest, payload = materialize_sync_files(manifest, sources)

    target_home = _home(tmp_path / "target-home")
    target_project = tmp_path / "target-project"
    target_project.mkdir()
    project_id = manifest.project_ids[0]
    apply_import(
        target_home,
        manifest,
        payload,
        environment_project_roots={project_id: target_project},
    )

    assert (target_project / "AGENTS.md").read_text() == "source rules\n"
    target_memory = (
        target_home
        / ".claude/projects"
        / sync_module._claude_project_slug(target_project)
        / "memory/MEMORY.md"
    )
    assert target_memory.read_text() == "learned\n"
    _, operation = _latest_operation(target_home)
    assert operation.target_roots == {project_id: str(target_project)}
    with pytest.raises(ValueError, match="project id"):
        replace(operation, target_roots={"bad": str(target_project)})
    with pytest.raises(ValueError, match="target must be absolute"):
        replace(operation, target_roots={project_id: "relative"})

    store = SnapshotStore(target_home / ".codelux")
    recovery = replace(
        operation,
        files=tuple(replace(item, state=FileState.RECOVERY_REQUIRED) for item in operation.files),
        state=OperationState.RECOVERY_REQUIRED,
    )
    store.write_manifest(recovery)
    store.write_recovery(recovery)
    store.recover(target_home, recovery.operation_id)
    assert not (target_project / "AGENTS.md").exists()
    assert not target_memory.exists()


def test_local_project_mcp_is_sanitized_and_merged_for_target_path(tmp_path: Path) -> None:
    source_home = _home(tmp_path / "source-home")
    source_project = tmp_path / "source-project"
    source_project.mkdir()
    (source_project / "AGENTS.md").write_text("rules\n")
    (source_home / ".claude.json").write_text(
        json.dumps(
            {
                "oauthAccount": {"token": "never"},
                "projects": {
                    str(source_project): {
                        "mcpServers": {
                            "docs": {
                                "command": "docs-server",
                                "env": {"PUBLIC_MODE": "on", "ACCESS_TOKEN": "never"},
                            }
                        },
                        "hasTrustDialogAccepted": True,
                    }
                },
            }
        )
    )
    manifest, sources = build_manifest(
        source_home,
        ["project_env", "local_env"],
        project_roots=(source_project,),
    )
    manifest, payload = materialize_sync_files(manifest, sources)
    assert set(json.loads(payload["project-local-mcp.json"])) == set(manifest.project_ids)
    assert b"ACCESS_TOKEN" not in payload["project-local-mcp.json"]
    assert b"oauthAccount" not in payload["project-local-mcp.json"]

    target_home = _home(tmp_path / "target-home")
    target_project = tmp_path / "target-project"
    target_project.mkdir()
    (target_home / ".claude.json").write_text(
        json.dumps({"theme": "dark", "projects": {str(target_project): {"visits": 2}}})
    )
    project_id = manifest.project_ids[0]
    apply_import(
        target_home,
        manifest,
        payload,
        environment_project_roots={project_id: target_project},
    )
    applied = json.loads((target_home / ".claude.json").read_text())
    assert applied["theme"] == "dark"
    assert applied["projects"][str(target_project)]["visits"] == 2
    assert applied["projects"][str(target_project)]["mcpServers"] == {
        "docs": {"command": "docs-server", "env": {"PUBLIC_MODE": "on"}}
    }
    baseline = next(iter(load_sync_state(target_home / ".codelux")["baselines"].values()))
    assert (
        baseline["files"]["project-local-mcp.json"]
        == hashlib.sha256((target_home / ".claude.json").read_bytes()).hexdigest()
    )

    (target_home / ".claude.json").write_text(
        json.dumps(
            {"projects": {str(target_project): {"mcpServers": {"docs": {"command": "different"}}}}}
        )
    )
    with pytest.raises(ValidationError, match="MCP conflict"):
        apply_import(
            target_home,
            manifest,
            payload,
            environment_project_roots={project_id: target_project},
        )
    apply_import(
        target_home,
        manifest,
        payload,
        overwrite={"claude": True},
        environment_project_roots={project_id: target_project},
    )


@pytest.mark.parametrize(
    "payload",
    [b"not-json", b"[]", b'{"projects": []}'],
)
def test_invalid_claude_local_project_state_is_rejected(tmp_path: Path, payload: bytes) -> None:
    home = _home(tmp_path / "home")
    project = tmp_path / "project"
    project.mkdir()
    (project / "AGENTS.md").write_text("rules\n")
    (home / ".claude.json").write_bytes(payload)
    with pytest.raises(ValidationError, match="local project configuration"):
        build_manifest(
            home,
            ["project_env", "local_env"],
            project_roots=(project,),
        )


def test_invalid_claude_local_mcp_shape_and_merge_targets_are_rejected(tmp_path: Path) -> None:
    source = tmp_path / "source.json"
    project = tmp_path / "project"
    project.mkdir()
    project_id = sync_module._project_id(project)
    source.write_text(json.dumps({"projects": {str(project): {"mcpServers": []}}}))
    with pytest.raises(ValidationError, match="MCP configuration"):
        sync_module._claude_local_mcp_content(source, {project_id: str(project)})
    with pytest.raises(ValidationError, match="MCP mapping"):
        sync_module._merge_claude_local_mcp(
            tmp_path / "missing.json", b'{"unknown": {}}', {project_id: str(project)}, False
        )

    target = tmp_path / "target.json"
    target.write_text('{"projects": []}')
    with pytest.raises(ValidationError, match="local project configuration"):
        sync_module._merge_claude_local_mcp(
            target, json.dumps({project_id: {}}).encode(), {project_id: str(project)}, False
        )


def test_unrelated_project_symlink_is_ignored_but_selected_symlink_is_rejected(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path / "home")
    project = tmp_path / "project"
    project.mkdir()
    (project / "AGENTS.md").write_text("rules\n")
    (project / "ordinary-link").symlink_to(project / "missing")
    build_manifest(home, ["project_env"], project_roots=(project,))
    (project / "AGENTS.md").unlink()
    (project / "AGENTS.md").symlink_to(project / "missing")
    with pytest.raises(ValidationError, match="symbolic link"):
        build_manifest(home, ["project_env"], project_roots=(project,))


def test_project_environment_requires_complete_safe_mapping(tmp_path: Path) -> None:
    source_home = _home(tmp_path / "source-home")
    source_project = tmp_path / "source-project"
    source_project.mkdir()
    (source_project / "AGENTS.md").write_text("rules\n")
    manifest, sources = build_manifest(
        source_home, ["project_env"], project_roots=(source_project,)
    )
    manifest, payload = materialize_sync_files(manifest, sources)
    target_home = _home(tmp_path / "target-home")

    with pytest.raises(ValidationError, match="requires a project root"):
        build_manifest(source_home, ["project_env"])
    with pytest.raises(ValidationError, match="requires project environment"):
        build_manifest(source_home, ["local_env"], project_roots=(source_project,))

    with pytest.raises(ValidationError, match="mapping is incomplete"):
        apply_import(target_home, manifest, payload)
    with pytest.raises(ValidationError, match="missing or unsafe"):
        apply_import(
            target_home,
            manifest,
            payload,
            environment_project_roots={manifest.project_ids[0]: tmp_path / "missing"},
        )


def test_project_and_user_logical_targets_reject_missing_or_unsafe_mapping(
    tmp_path: Path,
) -> None:
    project_id = "p-0123456789abcdef01234567"
    with pytest.raises(ValidationError, match="environment mapping"):
        sync_module._logical_target(tmp_path, f"project-env/{project_id}/AGENTS.md")
    with pytest.raises(ValidationError, match="environment target"):
        sync_module._logical_target(
            tmp_path, f"project-env/{project_id}/AGENTS.md", {project_id: "relative"}
        )
    with pytest.raises(ValidationError, match="memory mapping"):
        sync_module._logical_target(tmp_path, f"project-memory/{project_id}/MEMORY.md")
    with pytest.raises(ValidationError, match="memory target"):
        sync_module._logical_target(
            tmp_path, f"project-memory/{project_id}/MEMORY.md", {project_id: "relative"}
        )
    with pytest.raises(ValidationError, match="user environment path"):
        sync_module._logical_target(tmp_path, "user-env/claude/rules/")


def test_select_claude_projects_keeps_only_requested_history(tmp_path: Path) -> None:
    home = _home(tmp_path / "source")
    for slug in ("one", "two"):
        project = home / ".claude/projects" / slug
        project.mkdir(parents=True)
        (project / "session.jsonl").write_text("{}\n")
    manifest, files = build_manifest(home, ["sessions"])
    payload = {logical: path.read_bytes() for logical, path in files}
    selected, selected_files = select_claude_projects(manifest, payload, ["one"])
    assert [item.path for item in selected.files] == ["claude/projects/one/session.jsonl"]
    assert set(selected_files) == {"claude/projects/one/session.jsonl"}


def test_select_codex_projects_removes_skipped_threads_and_jsonl(tmp_path: Path) -> None:
    home = _home(tmp_path)
    database = home / ".codex/state_5.sqlite"
    sessions = home / ".codex/sessions/2026/08"
    sessions.mkdir(parents=True)
    paths = {
        "/source/one": sessions / "one.jsonl",
        "/source/two": sessions / "two.jsonl",
    }
    for path in paths.values():
        path.write_text("{}\n")
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE threads (id TEXT PRIMARY KEY, model_provider TEXT, "
            "rollout_path TEXT, cwd TEXT)"
        )
        connection.executemany(
            "INSERT INTO threads VALUES (?, 'custom', ?, ?)",
            [(cwd, str(path), cwd) for cwd, path in paths.items()],
        )
    manifest, files = build_manifest(home, ["sessions"], clients=("codex",))
    manifest, payload = sync_module.materialize_sync_files(manifest, files)
    selected, selected_files = select_codex_projects(manifest, payload, ["/source/one"])
    assert {entry.path for entry in selected.files} == {
        "codex/sessions/2026/08/one.jsonl",
        "codex/state_5.sqlite",
    }
    with tmp_path.joinpath("selected.sqlite").open("wb") as handle:
        handle.write(selected_files["codex/state_5.sqlite"])
    with sqlite3.connect(tmp_path / "selected.sqlite") as connection:
        assert connection.execute("SELECT cwd FROM threads").fetchall() == [("/source/one",)]
    skipped, skipped_files = select_codex_projects(manifest, payload, [])
    assert not skipped.files
    assert not skipped_files


def test_select_codex_projects_removes_related_rows_before_threads(tmp_path: Path) -> None:
    home = _home(tmp_path)
    database = home / ".codex/state_5.sqlite"
    sessions = home / ".codex/sessions/2026/08"
    sessions.mkdir(parents=True)
    kept_session = sessions / "kept.jsonl"
    skipped_session = sessions / "skipped.jsonl"
    kept_session.write_text("{}\n")
    skipped_session.write_text("{}\n")
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            "CREATE TABLE threads (id TEXT PRIMARY KEY, model_provider TEXT, "
            "rollout_path TEXT, cwd TEXT)"
        )
        connection.execute(
            "CREATE TABLE thread_dynamic_tools (thread_id TEXT REFERENCES threads(id))"
        )
        connection.execute(
            "CREATE TABLE thread_spawn_edges (parent_thread_id TEXT REFERENCES threads(id), "
            "child_thread_id TEXT REFERENCES threads(id))"
        )
        connection.executemany(
            "INSERT INTO threads VALUES (?, 'custom', ?, ?)",
            [
                ("kept", str(kept_session), "/source/kept"),
                ("skipped", str(skipped_session), "/source/skipped"),
            ],
        )
        connection.executemany(
            "INSERT INTO thread_dynamic_tools VALUES (?)", [("kept",), ("skipped",)]
        )
        connection.executemany(
            "INSERT INTO thread_spawn_edges VALUES (?, ?)",
            [("kept", "kept"), ("kept", "skipped")],
        )
    manifest, files = build_manifest(home, ["sessions"], clients=("codex",))
    manifest, payload = sync_module.materialize_sync_files(manifest, files)

    _, selected_files = select_codex_projects(manifest, payload, ["/source/kept"])

    selected_database = tmp_path / "selected-related.sqlite"
    selected_database.write_bytes(selected_files["codex/state_5.sqlite"])
    with sqlite3.connect(selected_database) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("SELECT id FROM threads").fetchall() == [("kept",)]
        assert connection.execute("SELECT thread_id FROM thread_dynamic_tools").fetchall() == [
            ("kept",)
        ]
        assert connection.execute("SELECT * FROM thread_spawn_edges").fetchall() == [
            ("kept", "kept")
        ]


def test_map_codex_sessions_rejects_duplicate_target_roots(tmp_path: Path) -> None:
    home = _home(tmp_path)
    with sqlite3.connect(home / ".codex/state_5.sqlite") as connection:
        connection.execute(
            "CREATE TABLE threads (id TEXT PRIMARY KEY, model_provider TEXT, "
            "rollout_path TEXT, cwd TEXT)"
        )
    manifest, files = build_manifest(home, ["sessions"], clients=("codex",))
    manifest, payload = sync_module.materialize_sync_files(manifest, files)
    target = tmp_path / "target"

    with pytest.raises(ValidationError, match="distinct target roots"):
        map_codex_sessions(
            manifest,
            payload,
            target,
            {"/source/one": target / "project", "/source/two": target / "project"},
        )


def test_collection_can_limit_config_to_one_client(tmp_path: Path) -> None:
    home = _home(tmp_path / "source")
    manifest, files = build_manifest(home, ["config"], clients=("claude",))
    assert manifest.selection == ("config",)
    assert [logical for logical, _ in files] == ["claude/settings.json"]


def test_collection_can_limit_session_history_to_one_client(tmp_path: Path) -> None:
    home = _home(tmp_path)
    claude = home / ".claude/projects/project"
    claude.mkdir(parents=True)
    (claude / "session.jsonl").write_text("{}\n")
    codex = home / ".codex/sessions/2026/08/11"
    codex.mkdir(parents=True)
    (codex / "session.jsonl").write_text("{}\n")

    _, claude_files = build_manifest(home, ["sessions"], clients=("claude",))
    _, codex_files = build_manifest(home, ["sessions"], clients=("codex",))
    assert [logical for logical, _ in claude_files] == ["claude/projects/project/session.jsonl"]
    assert [logical for logical, _ in codex_files] == ["codex/sessions/2026/08/11/session.jsonl"]


def test_collection_rejects_invalid_selection_and_unsafe_sensitive_paths(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path / "source")
    with pytest.raises(ValidationError, match="supported sync selection"):
        sync_module.collect_files(home, [])

    projects = home / ".claude/projects"
    projects.symlink_to(tmp_path)
    with pytest.raises(ValidationError, match="symbolic link"):
        sync_module.collect_files(home, ["sessions"])
    projects.unlink()

    auth = home / ".codex/auth.json"
    auth.write_text("not-json")
    with pytest.raises(ValidationError, match="authentication files are invalid"):
        sync_module.collect_files(home, ["config"], include_keys=True)
    auth.write_text('{"auth_mode":"apikey","OPENAI_API_KEY":"secret"}\n')
    (home / ".codex/config.toml").write_text('model_provider = "custom"\n')
    hardlink = tmp_path / "auth-hardlink.json"
    os.link(auth, hardlink)
    with pytest.raises(ValidationError, match="multiple hard links"):
        sync_module.collect_files(home, ["config"], include_keys=True)


def test_manifest_limits_and_file_size_are_fail_closed(tmp_path: Path, monkeypatch) -> None:
    home = _home(tmp_path / "source")
    monkeypatch.setattr(sync_module, "MAX_FILES", 0)
    with pytest.raises(ValidationError, match="file count"):
        build_manifest(home, ["config"])
    monkeypatch.setattr(sync_module, "MAX_FILES", 100_000)
    monkeypatch.setattr(sync_module, "MAX_TOTAL", 0)
    with pytest.raises(ValidationError, match="4 GiB"):
        build_manifest(home, ["config"])
    monkeypatch.setattr(sync_module, "MAX_FILE", 1)
    with pytest.raises(ValidationError, match="256 MiB"):
        sync_module._hash_file(home / ".claude/settings.json")


def test_claude_mapping_validation_and_non_json_preservation(tmp_path: Path) -> None:
    home = _home(tmp_path / "source")
    project = home / ".claude/projects/source"
    project.mkdir(parents=True)
    session = project / "session.jsonl"
    session.write_bytes(b"not-json\n" + b'{"cwd":"/old"}\n')
    manifest, files = build_manifest(home, ["sessions"])
    payload = {logical: path.read_bytes() for logical, path in files}
    with pytest.raises(ValidationError, match="mapping is incomplete"):
        sync_module.map_claude_sessions(manifest, payload, {"wrong": Path("/target")})
    with pytest.raises(ValidationError, match="must be absolute"):
        sync_module.map_claude_sessions(manifest, payload, {"source": Path("relative")})
    with pytest.raises(ValidationError, match="unknown Claude source project"):
        sync_module.select_claude_project(manifest, payload, "missing")

    mapped, mapped_files = sync_module.map_claude_sessions(
        manifest, payload, {"source": Path("/target")}
    )
    logical = next(item.path for item in mapped.files)
    assert mapped_files[logical].startswith(b"not-json\n")
    assert b'"cwd":"/target"' in mapped_files[logical]


@pytest.mark.parametrize(
    ("entries", "message"),
    [
        ([("other", b"x")], "manifest.json must be the first"),
        ([("manifest.json", b"not-json")], "manifest is invalid"),
    ],
)
def test_plain_archive_rejects_invalid_manifest_layout(
    entries: list[tuple[str, bytes]], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        parse_plain_archive(_plain_tar(entries))


def test_plain_archive_rejects_unexpected_missing_unsafe_and_mismatched_members(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path / "source")
    manifest, files = build_manifest(home, ["config"])
    manifest_raw = json.dumps(manifest.to_dict()).encode()
    first_logical, first_path = files[0]
    first_content = first_path.read_bytes()
    declared_one = replace(manifest, files=(manifest.files[0],))
    declared_raw = json.dumps(declared_one.to_dict()).encode()

    with pytest.raises(ValidationError, match="unexpected member"):
        parse_plain_archive(_plain_tar([("manifest.json", manifest_raw), ("unexpected", b"x")]))
    with pytest.raises(ValidationError, match="missing a declared member"):
        parse_plain_archive(_plain_tar([("manifest.json", declared_raw)]))
    with pytest.raises(ValidationError, match="path is unsafe"):
        parse_plain_archive(_plain_tar([("manifest.json", manifest_raw), ("../escape", b"x")]))
    with pytest.raises(ValidationError, match="hash mismatch"):
        parse_plain_archive(
            _plain_tar([("manifest.json", declared_raw), (first_logical, b"X" + first_content[1:])])
        )


def test_sqlite_validation_and_sidecar_safety(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="malformed"):
        sync_module._validate_sqlite_bytes(b"not sqlite")
    database = tmp_path / "invalid.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE other (id TEXT)")
    content = database.read_bytes()
    with pytest.raises(ValidationError, match="incompatible threads table"):
        sync_module._validate_sqlite_bytes(content)

    valid = tmp_path / "valid.sqlite"
    with sqlite3.connect(valid) as connection:
        connection.execute("CREATE TABLE threads (id TEXT, model_provider TEXT)")
    valid_content = valid.read_bytes()
    target = tmp_path / "target.sqlite"
    sidecar = Path(str(target) + "-wal")
    sidecar.symlink_to(database)
    with pytest.raises(ValidationError, match="sidecar is unsafe"):
        sync_module._atomic_write_sqlite(target, valid_content, tmp_path)


def test_peer_baseline_and_provider_merge_validation(tmp_path: Path) -> None:
    home = _home(tmp_path / "source")
    manifest, _ = build_manifest(home, ["config"])
    root = tmp_path / "target/.codelux"
    sync_module.save_peer_baseline(root, "peer", manifest, {"file": b"content"})
    assert load_sync_state(root)["baselines"]["peer:config"]["files"]["file"]

    target = tmp_path / "providers.json"
    target.write_text("not-json")
    with pytest.raises(ValidationError, match="Registry is invalid"):
        sync_module._merge_provider_registry(target, b"{}", False)
    target.write_text('{"providers":{},"current":{}}')
    with pytest.raises(ValidationError, match="Registry is invalid"):
        sync_module._merge_provider_registry(target, b'{"providers":[]}', False)


def test_provider_merge_decision_matrix(tmp_path: Path) -> None:
    target = tmp_path / "providers.json"
    incoming = {
        "schema_version": 1,
        "providers": {"shared": {"name": "shared", "clients": {"codex": {"new": True}}}},
        "current": {"codex": "source-choice"},
    }

    assert (
        json.loads(
            sync_module._merge_provider_registry(target, json.dumps(incoming).encode(), False)
        )
        == incoming
    )

    target.write_text('{"schema_version":1,"providers":[],"current":{}}')
    with pytest.raises(ValidationError, match="Registry is invalid"):
        sync_module._merge_provider_registry(target, json.dumps(incoming).encode(), False)

    target.write_text('{"schema_version":1,"providers":{},"current":{}}')
    invalid_clients = dict(incoming)
    invalid_clients["providers"] = {"shared": {"name": "shared", "clients": []}}
    with pytest.raises(ValidationError, match="Registry is invalid"):
        sync_module._merge_provider_registry(target, json.dumps(invalid_clients).encode(), False)

    existing = {
        "schema_version": 1,
        "providers": {"shared": {"name": "shared", "clients": {"codex": {"old": True}}}},
        "current": {"claude": "local-choice"},
    }
    target.write_text(json.dumps(existing))
    with pytest.raises(ValidationError, match="Provider binding conflict"):
        sync_module._merge_provider_registry(target, json.dumps(incoming).encode(), False)

    merged = json.loads(
        sync_module._merge_provider_registry(target, json.dumps(incoming).encode(), True)
    )
    assert merged["providers"]["shared"] == incoming["providers"]["shared"]
    assert merged["current"] == existing["current"]


def test_import_replaces_sqlite_without_stale_wal_or_shm(tmp_path: Path) -> None:
    source = _home(tmp_path / "source")
    target = _home(tmp_path / "target")
    for home, title in ((source, "source"), (target, "target")):
        database = home / ".codex/state_5.sqlite"
        with sqlite3.connect(database) as connection:
            connection.execute("CREATE TABLE threads (id TEXT, model_provider TEXT)")
            connection.execute("INSERT INTO threads VALUES (?, ?)", (title, "custom"))
    target_db = target / ".codex/state_5.sqlite"
    Path(str(target_db) + "-wal").write_bytes(b"stale wal")
    Path(str(target_db) + "-shm").write_bytes(b"stale shm")
    manifest, files = build_manifest(source, ["sessions"])
    payload = {
        logical: (
            path.read_bytes()
            if logical != "codex/state_5.sqlite"
            else next(entry for entry in files if entry[0] == logical)[1].read_bytes()
        )
        for logical, path in files
    }
    # build_manifest records a Backup API digest, so feed the matching validated
    # snapshot rather than the live database bytes.
    from codelux.sync import _sqlite_backup_bytes

    payload["codex/state_5.sqlite"] = _sqlite_backup_bytes(source / ".codex/state_5.sqlite")
    apply_import(target, manifest, payload, overwrite=True)
    assert not Path(str(target_db) + "-wal").exists()
    assert not Path(str(target_db) + "-shm").exists()
    with sqlite3.connect(target_db) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("SELECT id FROM threads").fetchone() == ("source",)


def test_import_rewrites_codex_rollout_paths_for_target_home(tmp_path: Path) -> None:
    source = _home(tmp_path / "source")
    target = _home(tmp_path / "target")
    source_db = source / ".codex/state_5.sqlite"
    source_project = source / "project"
    target_project = target / "project"
    session = source / ".codex/sessions/2026/08/session.jsonl"
    session.parent.mkdir(parents=True)
    session.write_text(json.dumps({"payload": {"cwd": str(source_project)}}) + "\n")
    with sqlite3.connect(source_db) as connection:
        connection.execute(
            "CREATE TABLE threads (id TEXT PRIMARY KEY, model_provider TEXT, "
            "rollout_path TEXT, cwd TEXT)"
        )
        connection.execute(
            "INSERT INTO threads VALUES (?, ?, ?, ?)",
            ("thread", "custom", str(session), str(source_project)),
        )
    manifest, files = build_manifest(source, ["sessions"])
    payload = {logical: path.read_bytes() for logical, path in files}
    payload["codex/state_5.sqlite"] = sync_module._sqlite_backup_bytes(source_db)
    apply_import(
        target,
        manifest,
        payload,
        overwrite=True,
        codex_project_roots={str(source_project): target_project},
    )
    with sqlite3.connect(target / ".codex/state_5.sqlite") as connection:
        rollout_path, cwd = connection.execute("SELECT rollout_path, cwd FROM threads").fetchone()
    assert rollout_path == str(target / ".codex/sessions/2026/08/session.jsonl")
    assert cwd == str(target_project)
    mapped_record = json.loads((target / ".codex/sessions/2026/08/session.jsonl").read_text())
    assert mapped_record["payload"]["cwd"] == str(target_project)


def test_map_codex_sessions_preserves_malformed_jsonl_lines(tmp_path: Path) -> None:
    source = _home(tmp_path / "source")
    target = _home(tmp_path / "target")
    source_db = source / ".codex/state_5.sqlite"
    source_project = source / "project"
    target_project = target / "project"
    session = source / ".codex/sessions/2026/08/session.jsonl"
    session.parent.mkdir(parents=True)
    malformed = b'{"payload":\n'
    valid = json.dumps({"payload": {"cwd": str(source_project)}}).encode() + b"\n"
    session.write_bytes(malformed + valid)
    with sqlite3.connect(source_db) as connection:
        connection.execute(
            "CREATE TABLE threads (id TEXT PRIMARY KEY, model_provider TEXT, "
            "rollout_path TEXT, cwd TEXT)"
        )
        connection.execute(
            "INSERT INTO threads VALUES (?, ?, ?, ?)",
            ("thread", "custom", str(session), str(source_project)),
        )
    manifest, files = build_manifest(source, ["sessions"], clients=("codex",))
    manifest, payload = sync_module.materialize_sync_files(manifest, files)

    _, mapped = map_codex_sessions(
        manifest,
        payload,
        target,
        {str(source_project): target_project},
    )

    mapped_session = mapped["codex/sessions/2026/08/session.jsonl"]
    assert mapped_session.startswith(malformed)
    assert json.loads(mapped_session.splitlines()[1])["payload"]["cwd"] == str(target_project)


def test_sqlite_backup_retries_transient_lock(tmp_path: Path, monkeypatch) -> None:
    home = _home(tmp_path)
    database = home / ".codex/state_5.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE threads (id TEXT, model_provider TEXT)")
        connection.execute("INSERT INTO threads VALUES ('thread', 'custom')")
    original_connect = sync_module.sqlite3.connect
    attempts = 0

    def flaky_connect(*args, **kwargs):
        nonlocal attempts
        if args and isinstance(args[0], str) and args[0].startswith("file:") and attempts < 3:
            attempts += 1
            raise sqlite3.OperationalError("database is locked")
        return original_connect(*args, **kwargs)

    monkeypatch.setattr(sync_module.sqlite3, "connect", flaky_connect)
    monkeypatch.setattr(sync_module.time, "sleep", lambda _: None)
    assert sync_module._sqlite_backup_bytes(database)
    assert attempts == 3


def test_sqlite_backup_retry_budget_is_bounded(tmp_path: Path, monkeypatch) -> None:
    home = _home(tmp_path)
    database = home / ".codex/state_5.sqlite"
    waits = []
    attempts = 0

    def locked_connect(*args, **kwargs):
        nonlocal attempts
        if args and isinstance(args[0], str) and args[0].startswith("file:"):
            attempts += 1
            raise sqlite3.OperationalError("database is locked")
        return sqlite3.connect(*args, **kwargs)

    monkeypatch.setattr(sync_module.sqlite3, "connect", locked_connect)
    monkeypatch.setattr(sync_module.time, "sleep", waits.append)

    with pytest.raises(ValidationError, match="database is locked"):
        sync_module._sqlite_backup_bytes(database)

    assert attempts == sync_module.SQLITE_BACKUP_ATTEMPTS
    assert waits == [
        sync_module.SQLITE_BACKUP_BACKOFF_SECONDS * attempt
        for attempt in range(1, sync_module.SQLITE_BACKUP_ATTEMPTS)
    ]
    assert sum(waits) == pytest.approx(4.5)


def test_import_maps_claude_sessions_to_target_project(tmp_path: Path) -> None:
    source = _home(tmp_path / "source")
    target = _home(tmp_path / "target")
    session_dir = source / ".claude/projects/-Users-source-project"
    session_dir.mkdir(parents=True)
    (session_dir / "session.jsonl").write_text('{"type":"message","cwd":"/Users/source/project"}\n')
    manifest, files = build_manifest(source, ["sessions"])
    payload = {logical: path.read_bytes() for logical, path in files}
    apply_import(target, manifest, payload, claude_project_root=Path("/workspace/example-project"))
    mapped = target / ".claude/projects/-workspace-example-project/session.jsonl"
    assert mapped.is_file()
    assert '"cwd":"/workspace/example-project"' in mapped.read_text()


def test_import_applies_files_and_records_baseline(tmp_path: Path) -> None:
    source = _home(tmp_path / "source")
    target = _home(tmp_path / "target")
    (source / ".claude/settings.json").write_text('{"source":true}\n')
    (target / ".claude/settings.json").unlink()
    (target / ".codex/config.toml").unlink()
    manifest, files = build_manifest(source, ["config"])
    missing = apply_import(
        target, manifest, {logical: path.read_bytes() for logical, path in files}
    )
    assert missing == ()
    assert (target / ".claude/settings.json").read_text() == '{"source":true}\n'
    assert load_sync_state(target / ".codelux")["baselines"]


def test_import_post_write_failure_restores_file(tmp_path: Path, monkeypatch) -> None:
    source = _home(tmp_path / "source")
    target = _home(tmp_path / "target")
    source_settings = source / ".claude/settings.json"
    target_settings = target / ".claude/settings.json"
    source_settings.write_text('{"source":true}\n')
    before = target_settings.read_bytes()
    manifest, files = build_manifest(source, ["config"])
    payload = {logical: path.read_bytes() for logical, path in files}
    real_chmod = Path.chmod
    injected = False

    def fail_after_write(path: Path, mode: int, *args, **kwargs) -> None:
        nonlocal injected
        if path == target_settings and path.read_bytes() == payload["claude/settings.json"]:
            if not injected:
                injected = True
                raise OSError("injected post-write chmod failure")
        real_chmod(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "chmod", fail_after_write)
    with pytest.raises(ValidationError, match="target was restored"):
        apply_import(target, manifest, payload, overwrite=True)

    assert target_settings.read_bytes() == before
    assert not (target / ".codelux/sync-state.json").exists()
    store, operation = _latest_operation(target)
    assert operation.state is OperationState.ROLLED_BACK
    assert {item.state for item in operation.files} == {FileState.ROLLED_BACK}
    assert not (store.root / "recovery.json").exists()


def test_import_backup_failure_is_controlled_and_does_not_touch_target(
    tmp_path: Path, monkeypatch
) -> None:
    source = _home(tmp_path / "source")
    target = _home(tmp_path / "target")
    (source / ".claude/settings.json").write_text('{"source":true}\n')
    target_settings = target / ".claude/settings.json"
    before = target_settings.read_bytes()
    manifest, files = build_manifest(source, ["config"])
    payload = {logical: path.read_bytes() for logical, path in files}
    real_write = sync_module.atomic_write_private

    def fail_backup(path, content, root, validator=None):
        if "backups" in path.parts and path.name == "settings.json":
            raise OSError("injected backup failure")
        return real_write(path, content, root, validator)

    monkeypatch.setattr(sync_module, "atomic_write_private", fail_backup)
    with pytest.raises(ValidationError, match="preparation failed"):
        apply_import(target, manifest, payload, overwrite=True)

    assert target_settings.read_bytes() == before
    assert not (target / ".codelux/sync-state.json").exists()
    assert not (target / ".codelux/recovery.json").exists()


def test_import_finalization_failure_restores_config_provider_sqlite_and_baseline(
    tmp_path: Path, monkeypatch
) -> None:
    source = _home(tmp_path / "source")
    target = _home(tmp_path / "target")
    (source / ".claude/settings.json").write_text('{"source":true}\n')
    source_registry = json.loads((source / ".codelux/providers.json").read_text())
    source_registry["providers"]["proxy"] = {
        "name": "proxy",
        "description": "",
        "clients": {
            "codex": {
                "enabled": True,
                "base_url": "https://proxy.example",
                "api_key": "secret",
            }
        },
    }
    (source / ".codelux/providers.json").write_text(json.dumps(source_registry))
    for home, title in ((source, "source"), (target, "target")):
        with sqlite3.connect(home / ".codex/state_5.sqlite") as connection:
            connection.execute("CREATE TABLE threads (id TEXT, model_provider TEXT)")
            connection.execute("INSERT INTO threads VALUES (?, ?)", (title, "custom"))
    state = target / ".codelux/sync-state.json"
    state.write_text('{"schema_version":1,"baselines":{"old:config":{}}}\n')
    targets = [
        target / ".claude/settings.json",
        target / ".codex/config.toml",
        target / ".codex/state_5.sqlite",
        target / ".codelux/providers.json",
        state,
    ]
    before = {path: path.read_bytes() for path in targets}
    manifest, files = build_manifest(source, ["config", "providers", "sessions"], include_keys=True)
    payload = {logical: path.read_bytes() for logical, path in files}
    payload["codex/state_5.sqlite"] = sync_module._sqlite_backup_bytes(
        source / ".codex/state_5.sqlite"
    )
    real_set_state = SnapshotStore.set_operation_state

    def fail_committed(self, operation, state_value):
        if state_value is OperationState.COMMITTED:
            raise OSError("injected manifest finalization failure")
        return real_set_state(self, operation, state_value)

    monkeypatch.setattr(SnapshotStore, "set_operation_state", fail_committed)
    with pytest.raises(ValidationError, match="target was restored"):
        apply_import(target, manifest, payload, overwrite=True)

    assert {path: path.read_bytes() for path in targets} == before
    with sqlite3.connect(target / ".codex/state_5.sqlite") as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("SELECT id FROM threads").fetchone() == ("target",)
    store, operation = _latest_operation(target)
    assert operation.state is OperationState.ROLLED_BACK
    assert "codelux/sync-state.json" in {item.source_path for item in operation.files}
    assert not (store.root / "recovery.json").exists()


def test_import_compensation_failure_blocks_writes_until_explicit_recovery(
    tmp_path: Path, monkeypatch
) -> None:
    from click.testing import CliRunner

    from codelux.adapters.claude import ClaudeAdapter
    from codelux.adapters.codex import CodexAdapter
    from codelux.cli import main

    source = _home(tmp_path / "source")
    target = _home(tmp_path / "target")
    source_settings = source / ".claude/settings.json"
    target_settings = target / ".claude/settings.json"
    target_codex = target / ".codex/config.toml"
    source_settings.write_text('{"source":true}\n')
    (source / ".codex/config.toml").write_text('model_provider = "custom"\n')
    before_settings = target_settings.read_bytes()
    before_codex = target_codex.read_bytes()
    manifest, files = build_manifest(source, ["config"])
    payload = {logical: path.read_bytes() for logical, path in files}
    real_write = sync_module.atomic_write_private
    settings_writes = 0

    def fail_commit_and_compensation(path, content, root, validator=None):
        nonlocal settings_writes
        if path == target_settings:
            settings_writes += 1
            if settings_writes == 2:
                raise OSError("injected compensation failure")
        if path == target_codex:
            raise OSError("injected second-file commit failure")
        return real_write(path, content, root, validator)

    monkeypatch.setattr(sync_module, "atomic_write_private", fail_commit_and_compensation)
    with pytest.raises(ValidationError, match="recovery is required"):
        apply_import(target, manifest, payload, overwrite=True)

    assert target_settings.read_bytes() == payload["claude/settings.json"]
    assert target_codex.read_bytes() == before_codex
    store, operation = _latest_operation(target)
    assert operation.state is OperationState.RECOVERY_REQUIRED
    assert (store.root / "recovery.json").is_file()

    monkeypatch.setattr(sync_module, "atomic_write_private", real_write)
    monkeypatch.setattr(ClaudeAdapter, "is_running", lambda self: ProcessState.NOT_RUNNING)
    monkeypatch.setattr(CodexAdapter, "is_running", lambda self: ProcessState.NOT_RUNNING)
    runner = CliRunner()
    env = {"CODELUX_TEST_HOME": str(target)}
    blocked = runner.invoke(main, ["sync", "machine-id", "rotate"], env=env)
    assert blocked.exit_code != 0
    assert "recovery is required" in blocked.output

    preview = runner.invoke(main, ["recover", "--dry-run"], env=env)
    assert preview.exit_code == 0, preview.output
    assert operation.operation_id in preview.output
    assert target_settings.read_bytes() == payload["claude/settings.json"]
    assert (store.root / "recovery.json").is_file()

    recovered = runner.invoke(main, ["recover"], env=env)
    assert recovered.exit_code == 0, recovered.output
    assert target_settings.read_bytes() == before_settings
    assert target_codex.read_bytes() == before_codex
    assert not (store.root / "recovery.json").exists()
    assert store.read_manifest(operation.operation_id).state is OperationState.ROLLED_BACK

    unblocked = runner.invoke(main, ["sync", "machine-id", "rotate"], env=env)
    assert unblocked.exit_code == 0, unblocked.output


def test_import_conflict_requires_overwrite(tmp_path: Path) -> None:
    source = _home(tmp_path / "source")
    target = _home(tmp_path / "target")
    (target / ".claude/settings.json").write_text('{"local":true}\n')
    manifest, files = build_manifest(source, ["config"])
    payload = {logical: path.read_bytes() for logical, path in files}
    with pytest.raises(ValidationError, match="conflicts"):
        apply_import(target, manifest, payload)
    apply_import(target, manifest, payload, overwrite=True)
    assert (target / ".claude/settings.json").read_bytes() == payload["claude/settings.json"]


def test_import_client_overwrite_policy_is_independent(tmp_path: Path) -> None:
    source = _home(tmp_path / "source")
    target = _home(tmp_path / "target")
    project = source / ".claude/projects/project"
    project.mkdir(parents=True)
    (project / "session.jsonl").write_text('{"source":true}\n')
    manifest, files = build_manifest(source, ["sessions"], clients=("claude",))
    target_project = target / ".claude/projects/project"
    target_project.mkdir(parents=True)
    (target_project / "session.jsonl").write_text('{"local":true}\n')
    apply_import(
        target,
        manifest,
        {logical: path.read_bytes() for logical, path in files},
        overwrite={"claude": True, "codex": False},
    )
    assert (target_project / "session.jsonl").read_bytes() == (
        project / "session.jsonl"
    ).read_bytes()


@pytest.mark.parametrize("baseline_exists", [False, True], ids=["no-baseline", "baseline"])
@pytest.mark.parametrize("target_state", ["missing", "incoming", "old", "diverged"])
@pytest.mark.parametrize("overwrite", [False, True], ids=["default", "overwrite"])
def test_import_baseline_conflict_decision_matrix(
    tmp_path: Path, baseline_exists: bool, target_state: str, overwrite: bool
) -> None:
    source = _home(tmp_path / "source")
    target = _home(tmp_path / "target")
    source_settings = source / ".claude/settings.json"
    target_settings = target / ".claude/settings.json"
    old = b'{"source":"old"}\n'
    incoming = b'{"source":"incoming"}\n'
    diverged = b'{"target":"diverged"}\n'
    source_settings.write_bytes(old)
    old_manifest, _ = build_manifest(source, ["config"])
    source_settings.write_bytes(incoming)
    manifest, files = build_manifest(source, ["config"])
    payload = {logical: path.read_bytes() for logical, path in files}
    if baseline_exists:
        save_baseline(target / ".codelux", old_manifest)
    baseline_path = target / ".codelux/sync-state.json"
    baseline_before = baseline_path.read_bytes() if baseline_path.exists() else None
    if target_state == "missing":
        target_settings.unlink()
        target_before = None
    else:
        target_before = {
            "incoming": incoming,
            "old": old,
            "diverged": diverged,
        }[target_state]
        target_settings.write_bytes(target_before)

    conflict = not overwrite and (
        (not baseline_exists and target_state in {"old", "diverged"})
        or (baseline_exists and target_state == "diverged")
    )
    if conflict:
        with pytest.raises(ValidationError, match="claude/settings.json"):
            apply_import(target, manifest, payload, overwrite=overwrite)
        assert target_settings.read_bytes() == target_before
        assert (baseline_path.read_bytes() if baseline_path.exists() else None) == baseline_before
        backups = target / ".codelux/backups"
        assert not backups.exists() or not any(backups.iterdir())
        assert not (target / ".codelux/recovery.json").exists()
        return

    missing = apply_import(target, manifest, payload, overwrite=overwrite)
    assert target_settings.read_bytes() == incoming
    assert ("claude/settings.json" in missing) is (baseline_exists and target_state == "missing")
    state = load_sync_state(target / ".codelux")
    key = f"{manifest.source_machine_id}:config"
    assert (
        state["baselines"][key]["files"]["claude/settings.json"]
        == hashlib.sha256(incoming).hexdigest()
    )
    store, operation = _latest_operation(target)
    assert operation.state is OperationState.COMMITTED
    assert not (store.root / "recovery.json").exists()


def test_reset_baseline_restores_first_sync_conflict_protection(tmp_path: Path) -> None:
    source = _home(tmp_path / "source")
    target = _home(tmp_path / "target")
    source_settings = source / ".claude/settings.json"
    target_settings = target / ".claude/settings.json"
    source_settings.write_text('{"source":1}\n')
    first_manifest, first_files = build_manifest(source, ["config"])
    first_payload = {logical: path.read_bytes() for logical, path in first_files}
    apply_import(target, first_manifest, first_payload, overwrite=True)
    target_settings.write_text('{"target":"local-change"}\n')
    source_settings.write_text('{"source":2}\n')
    second_manifest, second_files = build_manifest(source, ["config"])
    second_payload = {logical: path.read_bytes() for logical, path in second_files}

    assert reset_baseline(target / ".codelux", first_manifest.source_machine_id, ("config",))
    before = target_settings.read_bytes()
    with pytest.raises(ValidationError, match="conflicts"):
        apply_import(target, second_manifest, second_payload)

    assert target_settings.read_bytes() == before
    assert not load_sync_state(target / ".codelux")["baselines"]


def test_identical_machine_id_clone_does_not_block_import(tmp_path: Path) -> None:
    source = _home(tmp_path / "source")
    target = _home(tmp_path / "target")
    (source / ".claude/settings.json").write_text('{"source":true}\n')
    manifest, files = build_manifest(source, ["config"])
    source_machine_id = (source / ".codelux/machine-id").read_bytes()
    (target / ".codelux/machine-id").write_bytes(source_machine_id)
    payload = {logical: path.read_bytes() for logical, path in files}

    apply_import(target, manifest, payload, overwrite=True)

    assert (target / ".claude/settings.json").read_bytes() == payload["claude/settings.json"]
    assert (target / ".codelux/machine-id").read_bytes() == source_machine_id


def test_provider_import_merges_without_switching_target_current(tmp_path: Path) -> None:
    source = _home(tmp_path / "source")
    target = _home(tmp_path / "target")
    source_registry = {
        "schema_version": 1,
        "providers": {
            "proxy": {
                "name": "proxy",
                "description": "",
                "clients": {
                    "codex": {
                        "enabled": True,
                        "base_url": "https://proxy.example",
                        "api_key": "secret",
                    }
                },
            }
        },
        "current": {"codex": "proxy"},
    }
    (source / ".codelux/providers.json").write_text(json.dumps(source_registry))
    target_registry = {
        "schema_version": 1,
        "providers": {},
        "current": {"codex": None, "claude": None},
    }
    (target / ".codelux/providers.json").write_text(json.dumps(target_registry))
    manifest, files = build_manifest(source, ["providers"], include_keys=True)
    apply_import(target, manifest, {logical: path.read_bytes() for logical, path in files})
    result = json.loads((target / ".codelux/providers.json").read_bytes())
    assert "proxy" in result["providers"]
    assert result["current"] == target_registry["current"]


def test_machine_id_rotate_clears_baselines(tmp_path: Path) -> None:
    root = tmp_path / ".codelux"
    root.mkdir()
    old = root / "machine-id"
    old.write_text("a" * 32 + "\n")
    state = root / "sync-state.json"
    state.write_text('{"schema_version":1,"baselines":{"remote:config":{}}}\n')
    value = rotate_machine_id(root)
    assert value != "a" * 32
    assert not state.exists()


def test_machine_identity_and_sync_state_validation(tmp_path: Path) -> None:
    root = tmp_path / ".codelux"
    root.mkdir()
    machine_path = root / "machine-id"
    machine_path.write_text("short\n")
    with pytest.raises(ValidationError, match="machine-id is invalid"):
        sync_module.machine_id(root)

    machine_path.unlink()
    machine_path.symlink_to(tmp_path / "elsewhere")
    with pytest.raises(ValidationError, match="symbolic link"):
        sync_module.machine_id(root)
    with pytest.raises(ValidationError, match="identity path is unsafe"):
        rotate_machine_id(root)

    machine_path.unlink()
    state_path = root / "sync-state.json"
    state_path.write_text("not-json")
    with pytest.raises(ValidationError, match="sync-state.json is invalid"):
        load_sync_state(root)
    state_path.write_text('{"schema_version":2,"baselines":{}}')
    with pytest.raises(ValidationError, match="sync-state.json is invalid"):
        load_sync_state(root)
    state_path.unlink()
    state_path.symlink_to(tmp_path / "elsewhere")
    with pytest.raises(ValidationError, match="sync-state.json is unsafe"):
        load_sync_state(root)


def test_reset_baseline_can_select_one_remote_selection(tmp_path: Path) -> None:
    root = tmp_path / ".codelux"
    root.mkdir()
    (root / "sync-state.json").write_text(
        '{"schema_version":1,"baselines":{"remote:config":{},"remote:sessions":{}}}\n'
    )
    assert reset_baseline(root, "remote", ("config",))
    assert set(load_sync_state(root)["baselines"]) == {"remote:sessions"}


def test_transport_capability_rejects_missing_selection(tmp_path: Path) -> None:
    capability = Capability((1,), ("config",), ("claude",), 256, 100, 1024, False, "remote", "1")
    home = _home(tmp_path / "source")
    manifest, _ = build_manifest(home, ["config", "providers"], include_keys=True)
    with pytest.raises(ValidationError, match="selections"):
        validate_capability(capability, manifest)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ({"protocol_versions": (2,)}, "protocol"),
        ({"supported_selections": ("config",)}, "selections"),
        ({"installed_clients": ("claude",)}, "not installed"),
        ({"supports_keys": False}, "credential"),
        ({"max_file_count": 0}, "file count"),
        ({"max_file_size": 0}, "per-file"),
        ({"max_total_size": 0}, "total-size"),
    ],
)
def test_transport_capability_rejects_each_negotiated_limit(
    tmp_path: Path, mutation: dict, message: str
) -> None:
    home = _home(tmp_path / "source")
    manifest, _ = build_manifest(home, ["config", "providers"], include_keys=True)
    capability = local_capability(home)

    with pytest.raises(ValidationError, match=message):
        validate_capability(replace(capability, **mutation), manifest)


@pytest.mark.parametrize(
    "raw",
    [
        {},
        {
            "protocol_versions": ["bad"],
            "supported_selections": [],
            "installed_clients": [],
            "max_file_size": 1,
            "max_file_count": 1,
            "max_total_size": 1,
            "supports_keys": True,
            "machine_id": "remote",
            "codelux_version": "1",
        },
        {
            "protocol_versions": [1],
            "supported_selections": [],
            "installed_clients": [],
            "max_file_size": 1,
            "max_file_count": 1,
            "max_total_size": 1,
            "supports_keys": "yes",
            "machine_id": "remote",
            "codelux_version": "1",
        },
    ],
)
def test_transport_capability_schema_is_fail_closed(raw: object) -> None:
    with pytest.raises(ValidationError, match="capability is invalid"):
        Capability.from_dict(raw)


@pytest.mark.parametrize("payload", [b"", b"not-json\n", b"x" * (16 * 1024 + 1)])
def test_read_capability_rejects_missing_malformed_or_oversized_line(payload: bytes) -> None:
    with pytest.raises(ValidationError, match="capability"):
        read_capability(io.BytesIO(payload))


@pytest.mark.parametrize(
    ("target", "args"),
    [
        ("-oProxyCommand=bad", ["receive"]),
        ("root@example.com\nother", ["receive"]),
        ("root@example.com", ["receive;bad"]),
        ("root@example.com", [""]),
    ],
)
def test_transport_command_rejects_unsafe_target_or_arguments(target: str, args: list[str]) -> None:
    with pytest.raises(ValidationError, match="invalid"):
        ssh_command(target, args)


class _FakeInput:
    def __init__(self) -> None:
        self.data = bytearray()
        self.closed = False

    def write(self, content: bytes) -> int:
        self.data.extend(content)
        return len(content)

    def close(self) -> None:
        self.closed = True


class _FakePushProcess:
    def __init__(self, stdout: bytes, stderr: bytes = b"", code: int = 0) -> None:
        self.stdin = _FakeInput()
        self.stdout = io.BytesIO(stdout)
        self.stderr = io.BytesIO(stderr)
        self.code = code
        self.killed = False

    def wait(self) -> int:
        return self.code

    def kill(self) -> None:
        self.killed = True


def test_push_archive_negotiates_streams_and_confirms_commit(tmp_path: Path, monkeypatch) -> None:
    home = _home(tmp_path / "source")
    manifest, files = build_manifest(home, ["providers"], include_keys=True)
    archive = create_plain_archive(manifest, files)
    capability = local_capability(home)
    process = _FakePushProcess(
        canonical_line(capability.to_dict()) + b'{"status":"committed","operation":"op"}\n'
    )
    calls = []
    commands = []

    def fake_popen(command, **kwargs):
        commands.append(command)
        return process

    monkeypatch.setattr(sync_transport.subprocess, "Popen", fake_popen)

    remote, response = push_archive(
        "root@example.com",
        manifest,
        archive,
        overwrite=True,
        claude_project_root="/workspace/example-project",
        progress=calls.append,
        environment_project_roots={"p-0123456789abcdef01234567": tmp_path / "target"},
    )

    assert remote == capability
    assert response["operation"] == "op"
    assert calls == [
        "Opening SSH connection...",
        "Remote capability check passed; sending archive...",
        "Archive sent; waiting for target commit...",
    ]
    assert "--project-map-stdin" in commands[0]
    assert str(tmp_path / "target") not in " ".join(commands[0])
    stream = io.BytesIO(bytes(process.stdin.data))
    assert read_path_payload(stream) == {"p-0123456789abcdef01234567": str(tmp_path / "target")}
    assert stream.read() == archive


@pytest.mark.parametrize(
    ("response", "stderr", "code", "message"),
    [
        (b'{"status":"committed"}\n', b"remote exploded", 2, "remote exploded"),
        (b"not-json\n", b"", 0, "response is invalid"),
        (b'{"status":"prepared"}\n', b"", 0, "did not confirm"),
    ],
)
def test_push_archive_rejects_remote_failures_and_invalid_acknowledgements(
    tmp_path: Path,
    monkeypatch,
    response: bytes,
    stderr: bytes,
    code: int,
    message: str,
) -> None:
    home = _home(tmp_path / "source")
    manifest, files = build_manifest(home, ["providers"], include_keys=True)
    capability = canonical_line(local_capability(home).to_dict())
    process = _FakePushProcess(capability + response, stderr, code)
    monkeypatch.setattr(sync_transport.subprocess, "Popen", lambda *args, **kwargs: process)

    with pytest.raises(ValidationError, match=message):
        push_archive("root@example.com", manifest, create_plain_archive(manifest, files), False)


def test_push_archive_kills_transport_when_capability_read_fails(
    tmp_path: Path, monkeypatch
) -> None:
    home = _home(tmp_path / "source")
    manifest, files = build_manifest(home, ["providers"], include_keys=True)
    process = _FakePushProcess(b"")
    monkeypatch.setattr(sync_transport.subprocess, "Popen", lambda *args, **kwargs: process)

    with pytest.raises(ValidationError, match="capability"):
        push_archive("root@example.com", manifest, create_plain_archive(manifest, files), False)
    assert process.killed


def test_pull_archive_rejects_invalid_selection_before_starting_ssh(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="supported sync selection"):
        pull_archive("root@example.com", tmp_path, ("unknown",), True)


def test_pull_archive_rejects_remote_credential_policy_change(tmp_path: Path, monkeypatch) -> None:
    source = _home(tmp_path / "source")
    target = _home(tmp_path / "target")
    manifest, files = build_manifest(source, ["config"], include_keys=False)
    output = canonical_line(local_capability(source).to_dict()) + create_plain_archive(
        manifest, files
    )
    monkeypatch.setattr(
        sync_transport.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, output, b""),
    )

    with pytest.raises(ValidationError, match="credential policy"):
        pull_archive("root@example.com", target, ("config",), True)


def test_transport_command_is_argument_safe() -> None:
    command = ssh_command("root@example.com", ["receive", "--protocol", "1"])
    assert command[-5:] == ["codelux", "sync", "transport", "receive", "--protocol", "1"][-5:]
    assert "shell=True" not in command


def test_project_path_payload_reads_bounded_json_line() -> None:
    payload = {"p-0123456789abcdef01234567": "/work/项目 name"}
    assert read_path_payload(io.BytesIO(canonical_line(payload))) == payload
    with pytest.raises(ValidationError, match="missing or too large"):
        read_path_payload(io.BytesIO(b""))
    with pytest.raises(ValidationError, match="payload is invalid"):
        read_path_payload(io.BytesIO(b"not-json\n"))


def test_remote_project_discovery_uses_fixed_command_and_validates_paths(
    tmp_path: Path, monkeypatch
) -> None:
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(
            command, 0, canonical_line(["/remote/project-one", "/remote/project-two"]), b""
        )

    monkeypatch.setattr(sync_transport.subprocess, "run", fake_run)
    assert discover_remote_project_candidates("root@example.com") == (
        Path("/remote/project-one"),
        Path("/remote/project-two"),
    )
    assert calls[0][0][-3:] == ["discover-projects", "--protocol", "1"]
    assert calls[0][1]["check"] is False

    invalid = (
        canonical_line(["relative/project"]),
        canonical_line(["/remote/project", "/remote/project"]),
        canonical_line({"path": "/remote/project"}),
        canonical_line(["/remote/project"]) + b"extra",
    )
    for output in invalid:
        monkeypatch.setattr(
            sync_transport.subprocess,
            "run",
            lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, output, b""),
        )
        with pytest.raises(ValidationError, match="discovery response"):
            discover_remote_project_candidates("root@example.com")


def test_remote_project_discovery_reports_ssh_failure(monkeypatch) -> None:
    monkeypatch.setattr(
        sync_transport.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 255, b"", b"remote command unavailable"
        ),
    )
    with pytest.raises(ValidationError, match="remote command unavailable"):
        discover_remote_project_candidates("root@example.com")


def test_pull_archive_uses_fixed_command_and_validates_stream(tmp_path: Path, monkeypatch) -> None:
    source = _home(tmp_path / "source")
    target = _home(tmp_path / "target")
    manifest, files = build_manifest(source, ["providers"], include_keys=True)
    archive = create_plain_archive(manifest, files)
    capability = Capability(
        (1,),
        ("config", "providers", "sessions"),
        ("claude", "codex"),
        256 * 1024 * 1024,
        100_000,
        4 * 1024 * 1024 * 1024,
        True,
        "remote",
        "1",
    )
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(
            command, 0, canonical_line(capability.to_dict()) + archive, b""
        )

    monkeypatch.setattr(sync_transport.subprocess, "run", fake_run)
    remote, restored, payload = pull_archive(
        "root@example.com",
        target,
        ("providers",),
        True,
        clients=("claude",),
        project_roots=(tmp_path / "remote project",),
    )

    assert remote == capability
    assert restored == manifest
    assert set(payload) == {"codelux/providers.json"}
    assert "--project-roots-stdin" in calls[0][0]
    assert str(tmp_path / "remote project") not in " ".join(calls[0][0])
    assert json.loads(calls[0][1]["input"]) == [str(tmp_path / "remote project")]
    assert calls[0][1]["check"] is False


def test_plain_archive_truncation_is_a_controlled_validation_error(tmp_path: Path) -> None:
    source = _home(tmp_path / "source")
    manifest, files = build_manifest(source, ["providers"], include_keys=True)
    archive = create_plain_archive(manifest, files)

    with pytest.raises(ValidationError, match="archive format"):
        parse_plain_archive(archive[:700])


def test_pull_archive_rejects_remote_extra_selection(tmp_path: Path, monkeypatch) -> None:
    source = _home(tmp_path / "source")
    target = _home(tmp_path / "target")
    manifest, files = build_manifest(source, ["config", "providers"], include_keys=True)
    capability = Capability(
        (1,),
        ("config", "providers", "sessions"),
        ("claude", "codex"),
        256 * 1024 * 1024,
        100_000,
        4 * 1024 * 1024 * 1024,
        True,
        "remote",
        "1",
    )
    output = canonical_line(capability.to_dict()) + create_plain_archive(manifest, files)
    monkeypatch.setattr(
        sync_transport.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, output, b""),
    )

    with pytest.raises(ValidationError, match="unexpected selection"):
        pull_archive("root@example.com", target, ("providers",), True)


def test_pull_archive_reports_remote_failure_without_local_write(
    tmp_path: Path, monkeypatch
) -> None:
    target = _home(tmp_path / "target")
    registry = target / ".codelux/providers.json"
    before = registry.read_bytes()
    monkeypatch.setattr(
        sync_transport.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 255, b"", b"ssh: connection lost"
        ),
    )

    with pytest.raises(ValidationError, match="connection lost"):
        pull_archive("root@example.com", target, ("providers",), True)

    assert registry.read_bytes() == before
    assert not (target / ".codelux/recovery.json").exists()


def test_pull_archive_rejects_missing_local_client(tmp_path: Path, monkeypatch) -> None:
    source = _home(tmp_path / "source")
    target = tmp_path / "target"
    (target / ".codelux").mkdir(parents=True)
    manifest, files = build_manifest(source, ["config"], include_keys=True)
    capability = Capability(
        (1,),
        ("config", "providers", "sessions"),
        ("claude", "codex"),
        256 * 1024 * 1024,
        100_000,
        4 * 1024 * 1024 * 1024,
        True,
        "remote",
        "1",
    )
    output = canonical_line(capability.to_dict()) + create_plain_archive(manifest, files)
    monkeypatch.setattr(
        sync_transport.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, output, b""),
    )

    with pytest.raises(ValidationError, match="not installed"):
        pull_archive("root@example.com", target, ("config",), True)


def test_transport_send_outputs_capability_then_archive(tmp_path: Path) -> None:
    from click.testing import CliRunner

    from codelux.cli import main

    home = _home(tmp_path / "source")
    registry_before = (home / ".codelux/providers.json").read_bytes()
    result = CliRunner().invoke(
        main,
        ["sync", "transport", "send", "--protocol", "1", "--providers", "--keys"],
        env={"CODELUX_TEST_HOME": str(home)},
    )

    assert result.exit_code == 0, result.output
    stream = io.BytesIO(result.stdout_bytes)
    capability = read_capability(stream)
    manifest, payload = sync_transport.decode_transport_archive(stream.read())
    assert capability.protocol_versions == (1,)
    assert manifest.selection == ("providers",)
    assert set(payload) == {"codelux/providers.json"}
    assert (home / ".codelux/providers.json").read_bytes() == registry_before
