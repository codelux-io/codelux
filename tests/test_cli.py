import json
import sqlite3
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

import codelux.cli as cli_module
from codelux import sync_transport
from codelux.adapters.claude import ClaudeAdapter
from codelux.adapters.codex import CodexAdapter
from codelux.cli import main
from codelux.errors import ValidationError
from codelux.models import ConfigState, ProcessState
from codelux.snapshots import SnapshotStore
from codelux.sync import (
    _claude_project_slug,
    build_manifest,
    create_plain_archive,
    load_sync_state,
    materialize_sync_files,
)
from codelux.sync_transport import local_capability, parse_plain_archive


def test_sync_selection_splits_clients_and_overwrite_prompts(monkeypatch) -> None:
    answers = iter([True, True, True, True, True, True, True])
    monkeypatch.setattr(cli_module.click, "confirm", lambda *args, **kwargs: next(answers))
    selected, clients, _ = cli_module._push_selection(False, False)
    assert selected == (
        "providers",
        "sessions",
        "project_env",
        "local_env",
        "user_env",
        "memory",
    )
    assert clients == ("claude", "codex")
    assert cli_module._overwrite_prompts(selected, clients, True) == (
        clients,
        ("memory", "project_env", "providers", "user_env"),
    )
    prompts = []
    approvals = iter([True, False, True, False, True, False])

    def confirm(prompt, **kwargs):
        prompts.append(prompt)
        return next(approvals)

    monkeypatch.setattr(cli_module.click, "confirm", confirm)
    assert cli_module._overwrite_prompts(selected, clients, False) == (
        ("claude",),
        ("providers", "user_env"),
    )
    assert prompts == [
        "Allow overwriting conflicting target Claude Code project history?",
        "Allow overwriting conflicting target Codex session history?",
        "Allow overwriting conflicting target Providers and API keys?",
        "Allow overwriting conflicting target project environment (including selected local overrides)?",
        "Allow overwriting conflicting target user-level agent environment?",
        "Allow overwriting conflicting target Claude project memory?",
    ]
    with pytest.raises(ValidationError, match="requires --project-env"):
        cli_module._push_selection(False, False, local_env=True)
    with pytest.raises(ValidationError, match="requires --project-env"):
        cli_module._sync_selection(False, False, False, local_env=True)


def test_derived_claude_storage_paths_are_filtered_without_removing_nested_projects() -> None:
    project = Path("/Users/example/work/project")
    nested = project / "nested"
    other = Path("/Users/example/work/other")
    derived = other / _claude_project_slug(project)
    sources = {
        "project": project,
        "nested": nested,
        "other": other,
        "derived": derived,
    }
    files = {
        f"claude/projects/{slug}/session.jsonl": json.dumps({"cwd": str(path)}).encode()
        for slug, path in sources.items()
    }
    files["claude/projects/unavailable/session.jsonl"] = b'{"type":"message"}\n'

    assert cli_module._filter_derived_claude_source_slugs(files, [*sources, "unavailable"]) == [
        "project",
        "nested",
        "other",
        "unavailable",
    ]


def test_codex_project_mapping_prompts_for_each_source_project(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / "state.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE threads (id TEXT, cwd TEXT)")
        connection.executemany(
            "INSERT INTO threads VALUES (?, ?)",
            [("one", "/source/one"), ("two", "/source/two")],
        )
    answers = iter(["/target/one", ""])
    monkeypatch.setattr(cli_module.click, "prompt", lambda *args, **kwargs: next(answers))
    mappings = cli_module._codex_project_mapping({"codex/state_5.sqlite": database.read_bytes()})
    assert mappings == {"/source/one": Path("/target/one")}


def test_project_directory_requires_real_absolute_path(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="real absolute path"):
        cli_module._project_directory("-Users-user-project", local=False)
    with pytest.raises(ValidationError, match="does not exist"):
        cli_module._project_directory(tmp_path / "missing", local=True)
    project = tmp_path / "project"
    project.mkdir()
    assert cli_module._project_directory(project, local=True) == project


def test_explicit_environment_project_mappings_are_identity_based(
    tmp_path: Path, monkeypatch
) -> None:
    source_one = tmp_path / "source-one"
    source_two = tmp_path / "source-two"
    target_one = tmp_path / "target-one"
    target_two = tmp_path / "target-two"
    for path in (source_one, source_two, target_one, target_two):
        path.mkdir()

    sources, mapping = cli_module._explicit_project_mappings(
        (
            f"{source_two}={target_two}",
            f"{source_one}={target_one}",
        ),
        source_local=True,
        target_local=True,
    )
    assert sources == (source_two, source_one)
    assert mapping == {
        cli_module._project_id(source_two): target_two,
        cli_module._project_id(source_one): target_one,
    }
    assert (
        cli_module._target_mapping_by_id(
            tuple(sorted(mapping)),
            tuple(f"{project_id}={mapping[project_id]}" for project_id in sorted(mapping)),
        )
        == mapping
    )

    with pytest.raises(ValidationError, match="distinct"):
        cli_module._explicit_project_mappings(
            (f"{source_one}={target_one}", f"{source_two}={target_one}"),
            source_local=True,
            target_local=True,
        )
    with pytest.raises(ValidationError, match="SOURCE=TARGET"):
        cli_module._explicit_project_mappings(
            (str(source_one),), source_local=True, target_local=True
        )
    with pytest.raises(ValidationError, match="absolute"):
        cli_module._project_directory("relative", local=False)
    with pytest.raises(ValidationError, match="does not exist"):
        cli_module._project_directory(tmp_path / "missing", local=True)

    project_ids = tuple(sorted(mapping))
    manifest = type("Manifest", (), {"project_ids": project_ids})()
    with pytest.raises(ValidationError, match="does not match"):
        cli_module._environment_target_mapping(manifest, (source_one,), (target_one,))
    one_manifest = type("Manifest", (), {"project_ids": (cli_module._project_id(source_one),)})()
    with pytest.raises(ValidationError, match="requires one target"):
        cli_module._environment_target_mapping(one_manifest, (source_one,), ())
    with pytest.raises(ValidationError, match="distinct targets"):
        cli_module._environment_target_mapping(
            manifest, (source_one, source_two), (target_one, target_one)
        )

    with pytest.raises(ValidationError, match="PROJECT_ID=TARGET"):
        cli_module._target_mapping_by_id(project_ids, (project_ids[0],))
    with pytest.raises(ValidationError, match="unknown or duplicate"):
        cli_module._target_mapping_by_id(project_ids, (f"p-{'f' * 24}={target_one}",))
    with pytest.raises(ValidationError, match="distinct targets"):
        cli_module._target_mapping_by_id(
            project_ids,
            (f"{project_ids[0]}={target_one}", f"{project_ids[1]}={target_one}"),
        )
    with pytest.raises(ValidationError, match="each project ID"):
        cli_module._target_mapping_by_id(project_ids, (f"{project_ids[0]}={target_one}",))

    prompt_answers = iter([str(target_one), "", str(target_one)])
    monkeypatch.setattr(cli_module.click, "prompt", lambda *args, **kwargs: next(prompt_answers))
    assert cli_module._environment_project_roots((), local=True, prompt="Project") == (target_one,)
    assert cli_module._environment_targets_for_sources((source_one,), (), local=True) == (
        target_one,
    )
    with pytest.raises(ValidationError, match="requires one target"):
        cli_module._environment_targets_for_sources(
            (source_one,), (target_one, target_two), local=True
        )


def _claude_home(tmp_path: Path) -> Path:
    settings_dir = tmp_path / ".claude"
    settings_dir.mkdir(parents=True)
    (settings_dir / "settings.json").write_text('{"env": {}, "keep": true}\n')
    return tmp_path


def test_add_allows_fresh_codex_install(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(CodexAdapter, "is_installed", lambda self: True)
    monkeypatch.setattr(CodexAdapter, "is_running", lambda self: ProcessState.NOT_RUNNING)
    result = CliRunner().invoke(
        main,
        ["add", "proxy", "--url", "https://proxy.example", "--client", "codex"],
        input="codex-secret\ncodex-secret\n",
        env={"CODELUX_TEST_HOME": str(tmp_path)},
    )

    assert result.exit_code == 0, result.output
    assert "added and activated proxy for codex" in result.output
    assert (tmp_path / ".codex/config.toml").is_file()
    assert json.loads((tmp_path / ".codex/auth.json").read_text())["auth_mode"] == "apikey"
    assert "proxy" in (tmp_path / ".codex/config.toml").read_text()


def test_cli_add_list_switch_and_restore_official(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(ClaudeAdapter, "is_running", lambda self: ProcessState.NOT_RUNNING)
    home = _claude_home(tmp_path)
    runner = CliRunner()
    env = {"CODELUX_TEST_HOME": str(home)}

    added = runner.invoke(
        main,
        ["add", "proxy", "--client", "claude"],
        input="https://proxy.example\ntest-secret\ntest-secret\n",
        env=env,
    )
    assert added.exit_code == 0, added.output

    status = runner.invoke(main, ["status", "--client", "claude"], env=env)
    assert status.exit_code == 0, status.output
    assert "claude: custom (provider=proxy, healthy, process=not_running)" in status.output

    listed = runner.invoke(main, ["list", "--format", "json"], env=env)
    assert listed.exit_code == 0
    rows = json.loads(listed.output)
    assert rows == [
        {
            "name": "official",
            "clients": ["claude", "codex"],
            "description": "Built-in official Provider",
            "builtin": True,
        },
        {
            "name": "proxy",
            "clients": ["claude"],
            "description": "",
            "builtin": False,
        },
    ]
    assert "test-secret" not in listed.output

    switched = runner.invoke(main, ["switch", "proxy", "--client", "claude"], env=env)
    assert switched.exit_code == 0, switched.output
    settings = json.loads((home / ".claude" / "settings.json").read_text())
    assert settings["env"]["ANTHROPIC_BASE_URL"] == "https://proxy.example"
    assert settings["keep"] is True

    restored = runner.invoke(main, ["switch", "official", "--client", "claude"], env=env)
    assert restored.exit_code == 0, restored.output
    settings = json.loads((home / ".claude" / "settings.json").read_text())
    assert settings["env"] == {} and settings["keep"] is True


def test_switch_official_without_snapshot_explains_native_login(
    tmp_path: Path, monkeypatch
) -> None:
    home = _claude_home(tmp_path)
    (home / ".codelux" / "backups").mkdir(parents=True)
    codex_dir = home / ".codex"
    codex_dir.mkdir()
    (codex_dir / "config.toml").write_text(
        'model_provider = "custom"\n\n[model_providers.custom]\nbase_url = "https://proxy.example"\n'
    )
    (codex_dir / "auth.json").write_text(
        json.dumps({"auth_mode": "apikey", "OPENAI_API_KEY": "test-token"})
    )
    monkeypatch.setattr(CodexAdapter, "is_running", lambda self: ProcessState.NOT_RUNNING)
    result = CliRunner().invoke(
        main,
        ["switch", "official", "--client", "codex"],
        env={"CODELUX_TEST_HOME": str(home)},
    )
    assert result.exit_code != 0
    assert result.output == (
        "Error: no verified official snapshot exists for codex.\n"
        "  Sign in to Codex with your official account:\n"
        "    codex login --device-auth\n"
    )


def test_switch_official_is_noop_after_native_codex_login(tmp_path: Path, monkeypatch) -> None:
    home = _claude_home(tmp_path)
    (home / ".codelux" / "backups").mkdir(parents=True)
    codex_dir = home / ".codex"
    codex_dir.mkdir()
    (codex_dir / "config.toml").write_text('model_provider = "openai"\n')
    (codex_dir / "auth.json").write_text(
        json.dumps({"auth_mode": "chatgpt", "tokens": {"access_token": "test-token"}})
    )
    monkeypatch.setattr(CodexAdapter, "is_running", lambda self: ProcessState.NOT_RUNNING)
    result = CliRunner().invoke(
        main,
        ["switch", "official", "--client", "codex"],
        env={"CODELUX_TEST_HOME": str(home)},
    )
    assert result.exit_code == 0, result.output
    assert result.output == "codex is already using the official configuration\n"


def test_switch_official_recovers_login_snapshot_mislabeled_unknown(
    tmp_path: Path, monkeypatch
) -> None:
    home = _claude_home(tmp_path)
    root = home / ".codelux"
    root.mkdir()
    (root / "providers.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "providers": {
                    "codelux": {
                        "name": "codelux",
                        "description": "",
                        "clients": {
                            "codex": {
                                "enabled": True,
                                "base_url": "https://codelux.example",
                                "api_key": "custom-key",
                                "wire_api": "responses",
                                "requires_openai_auth": True,
                            }
                        },
                    }
                },
                "current": {},
            }
        )
    )
    codex_dir = home / ".codex"
    codex_dir.mkdir()
    (codex_dir / "config.toml").write_text(
        'model_provider = "custom"\n\n'
        "[model_providers.custom]\n"
        'name = "custom"\n'
        'base_url = "https://old.example"\n'
        'wire_api = "responses"\n'
        "requires_openai_auth = true\n"
    )
    (codex_dir / "auth.json").write_text(
        json.dumps({"auth_mode": "chatgpt", "tokens": {"access_token": "test-token"}})
    )
    sessions = codex_dir / "sessions" / "2026" / "08" / "10"
    sessions.mkdir(parents=True)
    session_file = sessions / "session.jsonl"
    session_file.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {"id": "session-id", "model_provider": "openai"},
            }
        )
        + "\n"
    )
    state_db = codex_dir / "state_5.sqlite"
    with sqlite3.connect(state_db) as conn:
        conn.execute("create table threads (id text primary key, model_provider text not null)")
        conn.execute("insert into threads values ('session-id', 'openai')")
        conn.commit()
    monkeypatch.setattr(CodexAdapter, "is_running", lambda self: ProcessState.NOT_RUNNING)
    runner = CliRunner()
    env = {"CODELUX_TEST_HOME": str(home)}

    switched_custom = runner.invoke(main, ["switch", "codelux", "--client", "codex"], env=env)
    assert switched_custom.exit_code == 0, switched_custom.output
    assert json.loads(session_file.read_text())["payload"]["model_provider"] == "custom"
    with sqlite3.connect(state_db) as conn:
        assert conn.execute("select model_provider from threads").fetchone()[0] == "custom"
    manifest_paths = list((root / "backups").glob("*/manifest.json"))
    assert len(manifest_paths) == 1
    manifest = json.loads(manifest_paths[0].read_text())
    assert manifest["before_states"]["codex"] == "official_login"
    manifest["before_states"]["codex"] = "unknown"
    manifest_paths[0].write_text(json.dumps(manifest))

    switched_official = runner.invoke(main, ["switch", "official", "--client", "codex"], env=env)
    assert switched_official.exit_code == 0, switched_official.output
    restored_config = (codex_dir / "config.toml").read_text()
    assert 'model_provider = "custom"' in restored_config
    custom_section = restored_config.split("[model_providers.custom]", 1)[1]
    assert 'name = "OpenAI"' in custom_section
    assert "base_url" not in custom_section
    assert json.loads((codex_dir / "auth.json").read_text())["auth_mode"] == "chatgpt"
    assert json.loads(session_file.read_text())["payload"]["model_provider"] == "custom"
    with sqlite3.connect(state_db) as conn:
        assert conn.execute("select model_provider from threads").fetchone()[0] == "custom"
    assert CodexAdapter(home).inspect().state is ConfigState.OFFICIAL_LOGIN

    switched_custom_again = runner.invoke(main, ["switch", "codelux", "--client", "codex"], env=env)
    assert switched_custom_again.exit_code == 0, switched_custom_again.output
    custom_config = (codex_dir / "config.toml").read_text()
    assert 'model_provider = "custom"' in custom_config
    custom_section = custom_config.split("[model_providers.custom]", 1)[1]
    assert 'name = "codelux"' in custom_section
    assert 'base_url = "https://codelux.example"' in custom_section
    assert json.loads((codex_dir / "auth.json").read_text())["auth_mode"] == "apikey"
    assert json.loads(session_file.read_text())["payload"]["model_provider"] == "custom"
    with sqlite3.connect(state_db) as conn:
        assert conn.execute("select model_provider from threads").fetchone()[0] == "custom"


def test_switch_official_without_snapshot_explains_claude_login(
    tmp_path: Path, monkeypatch
) -> None:
    home = _claude_home(tmp_path)
    (home / ".claude" / "settings.json").write_text(
        json.dumps(
            {
                "env": {
                    "ANTHROPIC_BASE_URL": "https://proxy.example",
                    "ANTHROPIC_AUTH_TOKEN": "test-token",
                }
            }
        )
    )
    (home / ".codelux" / "backups").mkdir(parents=True)
    monkeypatch.setattr(ClaudeAdapter, "is_running", lambda self: ProcessState.NOT_RUNNING)
    result = CliRunner().invoke(
        main,
        ["switch", "official", "--client", "claude"],
        env={"CODELUX_TEST_HOME": str(home)},
    )
    assert result.exit_code != 0
    assert result.output == (
        "Error: no verified official snapshot exists for claude.\n"
        "  Claude is still using third-party routing; running claude cannot start official login.\n"
        "  Register or adopt the current Provider, then rerun:\n"
        "    codelux switch official --client claude\n"
    )


def test_switch_official_prepares_native_claude_login_for_registered_provider(
    tmp_path: Path, monkeypatch
) -> None:
    home = _claude_home(tmp_path)
    settings = home / ".claude/settings.json"
    settings.write_text(
        json.dumps(
            {
                "env": {
                    "ANTHROPIC_BASE_URL": "https://proxy.example",
                    "ANTHROPIC_AUTH_TOKEN": "test-token",
                    "KEEP": "value",
                },
                "theme": "dark",
            }
        )
    )
    root = home / ".codelux"
    root.mkdir()
    (root / "providers.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "providers": {
                    "proxy": {
                        "name": "proxy",
                        "description": "",
                        "clients": {
                            "claude": {
                                "enabled": True,
                                "base_url": "https://proxy.example",
                                "api_key": "test-token",
                            }
                        },
                    }
                },
                "current": {"claude": "proxy"},
            }
        )
    )
    monkeypatch.setattr(ClaudeAdapter, "is_running", lambda self: ProcessState.NOT_RUNNING)
    result = CliRunner().invoke(
        main,
        ["switch", "official", "--client", "claude"],
        env={"CODELUX_TEST_HOME": str(home)},
    )
    assert result.exit_code == 0, result.output
    assert result.output == (
        "prepared Claude for official login; sign in with your official account:\n" "  claude\n"
    )
    updated = json.loads(settings.read_text())
    assert updated == {"env": {"KEEP": "value"}, "theme": "dark"}
    registry = json.loads((root / "providers.json").read_text())
    assert "proxy" in registry["providers"]
    assert registry["current"]["claude"] is None


def test_list_includes_official_when_registry_is_empty(tmp_path: Path) -> None:
    _claude_home(tmp_path)
    env = {"CODELUX_TEST_HOME": str(tmp_path)}
    runner = CliRunner()

    table = runner.invoke(main, ["list"], env=env)
    assert table.exit_code == 0, table.output
    assert table.output == "official [builtin]: claude, codex\n"

    machine_readable = runner.invoke(main, ["list", "--format", "json"], env=env)
    assert machine_readable.exit_code == 0, machine_readable.output
    assert json.loads(machine_readable.output) == [
        {
            "name": "official",
            "clients": ["claude", "codex"],
            "description": "Built-in official Provider",
            "builtin": True,
        }
    ]


def test_status_without_client_lists_all_installed_clients(tmp_path: Path) -> None:
    _claude_home(tmp_path)
    codex = tmp_path / ".codex"
    codex.mkdir()
    (codex / "config.toml").write_text('model_provider = "openai"\n')
    (codex / "auth.json").write_text(
        json.dumps({"auth_mode": "apikey", "OPENAI_API_KEY": "test-secret"})
    )

    result = CliRunner().invoke(main, ["status"], env={"CODELUX_TEST_HOME": str(tmp_path)})
    assert result.exit_code == 0, result.output
    assert "claude:" in result.output and "codex:" in result.output


def test_provider_command_inventory_is_minimal() -> None:
    result = CliRunner().invoke(main, ["--help"])
    assert result.exit_code == 0, result.output
    for command in ("add", "list", "recover", "remove", "status", "switch", "update"):
        assert command in result.output
    for removed in ("config", "edit", "reconcile", "rename"):
        assert f"  {removed} " not in result.output


def test_version_command_reports_package_version() -> None:
    result = CliRunner().invoke(main, ["version"])
    assert result.exit_code == 0, result.output
    assert "codelux version" in result.output


def test_sync_export_and_import_provider_archive_end_to_end(tmp_path: Path) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    for home in (source, target):
        (home / ".codelux").mkdir(parents=True)
    source_registry = {
        "schema_version": 1,
        "providers": {
            "proxy": {
                "name": "proxy",
                "description": "",
                "clients": {
                    "claude": {
                        "enabled": True,
                        "base_url": "https://proxy.example",
                        "api_key": "secret",
                    }
                },
            }
        },
        "current": {"claude": "proxy"},
    }
    empty_registry = {"schema_version": 1, "providers": {}, "current": {}}
    (source / ".codelux/providers.json").write_text(json.dumps(source_registry))
    (target / ".codelux/providers.json").write_text(json.dumps(empty_registry))
    archive = tmp_path / "providers.cdlx"
    runner = CliRunner()

    exported = runner.invoke(
        main,
        [
            "sync",
            "export",
            "--output",
            str(archive),
            "--providers",
            "--password-stdin",
        ],
        input="correct horse battery\n",
        env={"CODELUX_TEST_HOME": str(source)},
    )
    assert exported.exit_code == 0, exported.output
    imported = runner.invoke(
        main,
        ["sync", "import", str(archive), "--password-stdin"],
        input="correct horse battery\n",
        env={"CODELUX_TEST_HOME": str(target)},
    )

    assert imported.exit_code == 0, imported.output
    assert "proxy" in json.loads((target / ".codelux/providers.json").read_text())["providers"]


def test_sync_export_rejects_missing_selection_and_short_password(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".codelux").mkdir(parents=True)
    runner = CliRunner()
    env = {"CODELUX_TEST_HOME": str(home)}
    missing = runner.invoke(
        main,
        ["sync", "export", "--output", str(tmp_path / "missing.cdlx")],
        env=env,
    )
    short = runner.invoke(
        main,
        [
            "sync",
            "export",
            "--output",
            str(tmp_path / "short.cdlx"),
            "--config",
            "--password-stdin",
        ],
        input="short\n",
        env=env,
    )
    assert missing.exit_code != 0 and "select at least one" in missing.output
    assert short.exit_code != 0 and "12 characters" in short.output


def test_sync_import_config_gate_confirmation_and_success(tmp_path: Path, monkeypatch) -> None:
    source = _claude_home(tmp_path / "source")
    target = _claude_home(tmp_path / "target")
    (source / ".claude/settings.json").write_text('{"source":true}\n')
    archive = tmp_path / "config.cdlx"
    runner = CliRunner()
    monkeypatch.setattr(ClaudeAdapter, "is_running", lambda self: ProcessState.NOT_RUNNING)
    monkeypatch.setattr(CodexAdapter, "is_installed", lambda self: False)
    exported = runner.invoke(
        main,
        [
            "sync",
            "export",
            "--output",
            str(archive),
            "--config",
            "--password-stdin",
        ],
        input="correct horse battery\n",
        env={"CODELUX_TEST_HOME": str(source)},
    )
    assert exported.exit_code == 0, exported.output

    gated = runner.invoke(
        main,
        ["sync", "import", str(archive), "--password-stdin"],
        input="correct horse battery\n",
        env={"CODELUX_TEST_HOME": str(target)},
    )
    rejected = runner.invoke(
        main,
        [
            "sync",
            "import",
            str(archive),
            "--password-stdin",
            "--apply-active-provider",
        ],
        input="correct horse battery\nn\n",
        env={"CODELUX_TEST_HOME": str(target)},
    )
    accepted = runner.invoke(
        main,
        [
            "sync",
            "import",
            str(archive),
            "--password-stdin",
            "--apply-active-provider",
            "--overwrite",
        ],
        input="correct horse battery\ny\n",
        env={"CODELUX_TEST_HOME": str(target)},
    )

    assert gated.exit_code != 0 and "requires --apply-active-provider" in gated.output
    assert rejected.exit_code != 0 and "was not confirmed" in rejected.output
    assert accepted.exit_code == 0, accepted.output
    assert (target / ".claude/settings.json").read_text() == '{"source":true}\n'


def test_sync_import_maps_claude_history_from_real_paths(tmp_path: Path, monkeypatch) -> None:
    source = _claude_home(tmp_path / "source")
    source_project_path = "/source/project"
    source_project = source / ".claude/projects/-source-project"
    source_project.mkdir(parents=True)
    (source_project / "session.jsonl").write_text(json.dumps({"cwd": source_project_path}) + "\n")
    manifest, paths = build_manifest(source, ["sessions"], clients=("claude",))
    manifest, payload = cli_module.materialize_sync_files(manifest, paths)
    target = _claude_home(tmp_path / "target")
    (target / ".codelux").mkdir()
    target_project = target / "work/project"
    target_project.mkdir(parents=True)
    archive = tmp_path / "sessions.cdlx"
    archive.write_bytes(b"test archive")
    monkeypatch.setattr(cli_module, "import_encrypted", lambda *args: (manifest, payload))
    monkeypatch.setattr(ClaudeAdapter, "is_running", lambda self: ProcessState.NOT_RUNNING)

    result = CliRunner().invoke(
        main,
        ["sync", "import", str(archive), "--password-stdin", "--overwrite"],
        input=f"test password\n{target_project}\n",
        env={"CODELUX_TEST_HOME": str(target)},
    )

    assert result.exit_code == 0, result.output
    mapped = target / ".claude/projects" / _claude_project_slug(target_project) / "session.jsonl"
    assert json.loads(mapped.read_text())["cwd"] == str(target_project)


def test_sync_export_import_maps_project_environment_without_source_path(
    tmp_path: Path, monkeypatch
) -> None:
    source_home = _claude_home(tmp_path / "source-home")
    target_home = _claude_home(tmp_path / "target-home")
    for home in (source_home, target_home):
        root = home / ".codelux"
        root.mkdir()
        (root / "providers.json").write_text(
            json.dumps({"schema_version": 1, "providers": {}, "current": {}})
        )
    source_project = tmp_path / "source-project"
    target_project = tmp_path / "target-project"
    source_project.mkdir()
    target_project.mkdir()
    (source_project / "AGENTS.md").write_text("portable rules\n")
    monkeypatch.setattr(ClaudeAdapter, "is_running", lambda self: ProcessState.NOT_RUNNING)
    monkeypatch.setattr(CodexAdapter, "is_installed", lambda self: False)
    archive = tmp_path / "environment.cdlx"
    runner = CliRunner()

    exported = runner.invoke(
        main,
        [
            "sync",
            "export",
            "--output",
            str(archive),
            "--project-env",
            "--project-root",
            str(source_project),
            "--password-stdin",
        ],
        input="correct horse battery\n",
        env={"CODELUX_TEST_HOME": str(source_home)},
    )
    assert exported.exit_code == 0, exported.output

    imported = runner.invoke(
        main,
        [
            "sync",
            "import",
            str(archive),
            "--target-project-root",
            str(target_project),
            "--password-stdin",
        ],
        input="correct horse battery\n",
        env={"CODELUX_TEST_HOME": str(target_home)},
    )
    assert imported.exit_code == 0, imported.output
    assert (target_project / "AGENTS.md").read_text() == "portable rules\n"


def test_sync_push_provider_success_and_registry_compensation(tmp_path: Path, monkeypatch) -> None:
    home = _claude_home(tmp_path)
    settings = home / ".claude/settings.json"
    settings.write_text(
        '{"env":{"ANTHROPIC_BASE_URL":"https://proxy.example",'
        '"ANTHROPIC_AUTH_TOKEN":"secret"}}\n'
    )
    root = home / ".codelux"
    root.mkdir()
    original = json.dumps({"schema_version": 1, "providers": {}, "current": {}}).encode()
    (root / "providers.json").write_bytes(original)
    monkeypatch.setattr(ClaudeAdapter, "is_running", lambda self: ProcessState.NOT_RUNNING)
    monkeypatch.setattr(CodexAdapter, "is_installed", lambda self: False)
    captured = []

    def succeed(target, manifest, archive, overwrite, progress=None):
        captured.append((target, manifest, archive, overwrite))
        if progress:
            progress("capability ok")
        return local_capability(home), {"status": "committed"}

    monkeypatch.setattr(cli_module, "push_archive", succeed)
    result = CliRunner().invoke(
        main,
        ["sync", "push", "--ssh", "root@example.com", "--providers"],
        env={"CODELUX_TEST_HOME": str(home)},
    )
    assert result.exit_code == 0, result.output
    assert captured and '"status": "committed"' in result.output

    adopted = (root / "providers.json").read_bytes()
    monkeypatch.setattr(
        cli_module,
        "push_archive",
        lambda *args, **kwargs: (_ for _ in ()).throw(ValidationError("injected push failure")),
    )
    failed = CliRunner().invoke(
        main,
        ["sync", "push", "--ssh", "root@example.com", "--providers"],
        env={"CODELUX_TEST_HOME": str(home)},
    )
    assert failed.exit_code != 0 and "injected push failure" in failed.output
    assert (root / "providers.json").read_bytes() == adopted


def test_sync_push_passes_confirmed_user_environment_overwrite_scope(
    tmp_path: Path, monkeypatch
) -> None:
    home = _claude_home(tmp_path)
    (home / ".codelux").mkdir()
    (home / ".codelux/providers.json").write_text(
        json.dumps({"schema_version": 1, "providers": {}, "current": {}})
    )
    monkeypatch.setattr(ClaudeAdapter, "is_running", lambda self: ProcessState.NOT_RUNNING)
    monkeypatch.setattr(CodexAdapter, "is_running", lambda self: ProcessState.NOT_RUNNING)
    captured = {}

    def succeed(
        target,
        manifest,
        archive,
        overwrite,
        progress=None,
        overwrite_scopes=(),
    ):
        captured["overwrite"] = overwrite
        captured["overwrite_scopes"] = overwrite_scopes
        return local_capability(home), {"status": "committed"}

    monkeypatch.setattr(cli_module, "push_archive", succeed)
    result = CliRunner().invoke(
        main,
        ["sync", "push", "--ssh", "root@example.com", "--user-env"],
        input="y\n",
        env={"CODELUX_TEST_HOME": str(home)},
    )

    assert result.exit_code == 0, result.output
    assert "Allow overwriting conflicting target user-level agent environment?" in result.output
    assert captured == {"overwrite": False, "overwrite_scopes": ("user_env",)}


def test_sync_push_transfers_mapped_project_environment(tmp_path: Path, monkeypatch) -> None:
    home = _claude_home(tmp_path / "home")
    root = home / ".codelux"
    root.mkdir()
    (root / "providers.json").write_text(
        json.dumps({"schema_version": 1, "providers": {}, "current": {}})
    )
    project = tmp_path / "source-project"
    project.mkdir()
    (project / "AGENTS.md").write_text("rules\n")
    monkeypatch.setattr(ClaudeAdapter, "is_running", lambda self: ProcessState.NOT_RUNNING)
    monkeypatch.setattr(CodexAdapter, "is_installed", lambda self: False)
    captured = {}

    def succeed(
        target,
        manifest,
        archive,
        overwrite,
        progress=None,
        environment_project_roots=None,
    ):
        captured["manifest"] = manifest
        captured["mapping"] = environment_project_roots
        return local_capability(home), {"status": "committed"}

    monkeypatch.setattr(cli_module, "push_archive", succeed)
    result = CliRunner().invoke(
        main,
        [
            "sync",
            "push",
            "--ssh",
            "root@example.com",
            "--project-env",
            "--project-root",
            str(project),
            "--target-project-root",
            "/target/project",
        ],
        env={"CODELUX_TEST_HOME": str(home)},
    )

    assert result.exit_code == 0, result.output
    manifest = captured["manifest"]
    project_id = manifest.project_ids[0]
    assert manifest.selection == ("project_env",)
    assert {item.path for item in manifest.files} == {f"project-env/{project_id}/AGENTS.md"}
    assert captured["mapping"] == {project_id: Path("/target/project")}


def test_sync_push_guides_multiple_project_environment_mappings(
    tmp_path: Path, monkeypatch
) -> None:
    home = _claude_home(tmp_path / "home")
    root = home / ".codelux"
    root.mkdir()
    (root / "providers.json").write_text(
        json.dumps({"schema_version": 1, "providers": {}, "current": {}})
    )
    projects = (tmp_path / "source-one", tmp_path / "source-two")
    for index, project in enumerate(projects, 1):
        project.mkdir()
        (project / "AGENTS.md").write_text(f"project {index}\n")
    history = home / ".claude/projects/example"
    history.mkdir(parents=True)
    for index, project in enumerate(projects, 1):
        (history / f"session-{index}.jsonl").write_text(json.dumps({"cwd": str(project)}) + "\n")
    monkeypatch.setattr(ClaudeAdapter, "is_running", lambda self: ProcessState.NOT_RUNNING)
    monkeypatch.setattr(CodexAdapter, "is_installed", lambda self: False)
    captured = {}

    def succeed(
        target,
        manifest,
        archive,
        overwrite,
        progress=None,
        environment_project_roots=None,
    ):
        captured["manifest"] = manifest
        captured["mapping"] = environment_project_roots
        return local_capability(home), {"status": "committed"}

    monkeypatch.setattr(cli_module, "push_archive", succeed)
    result = CliRunner().invoke(
        main,
        ["sync", "push", "--ssh", "root@example.com", "--project-env"],
        input="y\ny\n\n/target/project-one\n/target/project-two\n",
        env={"CODELUX_TEST_HOME": str(home)},
    )

    assert result.exit_code == 0, result.output
    manifest = captured["manifest"]
    expected_mapping = {
        cli_module._project_id(projects[0]): Path("/target/project-one"),
        cli_module._project_id(projects[1]): Path("/target/project-two"),
    }
    assert set(manifest.project_ids) == set(expected_mapping)
    assert captured["mapping"] == expected_mapping
    assert f"Include suggested source project 1: {projects[0]}? [y/N]:" in result.output
    assert f"Include suggested source project 2: {projects[1]}? [y/N]:" in result.output
    assert "Additional source project directory (leave empty to finish):" in result.output


def test_sync_pull_applies_mapped_project_environment(tmp_path: Path, monkeypatch) -> None:
    source_home = _claude_home(tmp_path / "source-home")
    (source_home / ".codelux").mkdir()
    source_project = tmp_path / "source-project"
    source_project.mkdir()
    (source_project / "CLAUDE.md").write_text("source instructions\n")
    manifest, sources = build_manifest(
        source_home, ["project_env"], project_roots=(source_project,)
    )
    manifest, payload = materialize_sync_files(manifest, sources)

    target_home = _claude_home(tmp_path / "target-home")
    target_root = target_home / ".codelux"
    target_root.mkdir()
    (target_root / "providers.json").write_text(
        json.dumps({"schema_version": 1, "providers": {}, "current": {}})
    )
    target_project = tmp_path / "target-project"
    target_project.mkdir()
    monkeypatch.setattr(ClaudeAdapter, "is_running", lambda self: ProcessState.NOT_RUNNING)
    monkeypatch.setattr(CodexAdapter, "is_installed", lambda self: False)
    captured = {}

    def receive(
        target,
        home,
        selection,
        include_keys,
        progress=None,
        clients=(),
        project_roots=(),
    ):
        captured["project_roots"] = project_roots
        return local_capability(source_home), manifest, payload

    monkeypatch.setattr(cli_module, "pull_archive", receive)
    monkeypatch.setattr(
        cli_module,
        "discover_remote_project_candidates",
        lambda target: (_ for _ in ()).throw(
            AssertionError("explicit project roots must skip remote discovery")
        ),
    )
    result = CliRunner().invoke(
        main,
        [
            "sync",
            "pull",
            "--ssh",
            "root@example.com",
            "--project-env",
            "--project-root",
            str(source_project),
            "--target-project-root",
            str(target_project),
            "--overwrite",
        ],
        env={"CODELUX_TEST_HOME": str(target_home)},
    )

    assert result.exit_code == 0, result.output
    assert captured["project_roots"] == (source_project,)
    assert (target_project / "CLAUDE.md").read_text() == "source instructions\n"


def test_sync_pull_guides_multiple_project_environment_mappings(
    tmp_path: Path, monkeypatch
) -> None:
    source_home = _claude_home(tmp_path / "source-home")
    (source_home / ".codelux").mkdir()
    source_projects = (tmp_path / "source-one", tmp_path / "source-two")
    for index, project in enumerate(source_projects, 1):
        project.mkdir()
        (project / "CLAUDE.md").write_text(f"source {index}\n")
    manifest, sources = build_manifest(source_home, ["project_env"], project_roots=source_projects)
    manifest, payload = materialize_sync_files(manifest, sources)

    target_home = _claude_home(tmp_path / "target-home")
    target_root = target_home / ".codelux"
    target_root.mkdir()
    (target_root / "providers.json").write_text(
        json.dumps({"schema_version": 1, "providers": {}, "current": {}})
    )
    target_projects = (tmp_path / "target-one", tmp_path / "target-two")
    for project in target_projects:
        project.mkdir()
    monkeypatch.setattr(ClaudeAdapter, "is_running", lambda self: ProcessState.NOT_RUNNING)
    monkeypatch.setattr(CodexAdapter, "is_installed", lambda self: False)
    captured = {}

    def receive(
        target,
        home,
        selection,
        include_keys,
        progress=None,
        clients=(),
        project_roots=(),
    ):
        captured["project_roots"] = project_roots
        return local_capability(source_home), manifest, payload

    monkeypatch.setattr(cli_module, "pull_archive", receive)
    monkeypatch.setattr(
        cli_module, "discover_remote_project_candidates", lambda target: source_projects
    )
    result = CliRunner().invoke(
        main,
        ["sync", "pull", "--ssh", "root@example.com", "--project-env", "--overwrite"],
        input=f"y\ny\n\n{target_projects[0]}\n{target_projects[1]}\n",
        env={"CODELUX_TEST_HOME": str(target_home)},
    )

    assert result.exit_code == 0, result.output
    assert captured["project_roots"] == source_projects
    assert f"Include suggested source project 1: {source_projects[0]}? [y/N]:" in result.output
    assert f"Include suggested source project 2: {source_projects[1]}? [y/N]:" in result.output
    assert (target_projects[0] / "CLAUDE.md").read_text() == "source 1\n"
    assert (target_projects[1] / "CLAUDE.md").read_text() == "source 2\n"


def test_sync_pull_falls_back_to_manual_paths_when_remote_discovery_is_unavailable(
    tmp_path: Path, monkeypatch
) -> None:
    home = _claude_home(tmp_path / "home")
    root = home / ".codelux"
    root.mkdir()
    (root / "providers.json").write_text(
        json.dumps({"schema_version": 1, "providers": {}, "current": {}})
    )
    monkeypatch.setattr(ClaudeAdapter, "is_running", lambda self: ProcessState.NOT_RUNNING)
    monkeypatch.setattr(CodexAdapter, "is_installed", lambda self: False)
    monkeypatch.setattr(
        cli_module,
        "discover_remote_project_candidates",
        lambda target: (_ for _ in ()).throw(
            sync_transport.RemoteProjectDiscoveryUnavailable("older remote")
        ),
    )
    captured = {}

    def roots(values, *, local, prompt, default_cwd=False, candidates=()):
        captured["candidates"] = candidates
        raise ValidationError("stop after fallback")

    monkeypatch.setattr(cli_module, "_environment_project_roots", roots)
    result = CliRunner().invoke(
        main,
        ["sync", "pull", "--ssh", "root@example.com", "--project-env"],
        env={"CODELUX_TEST_HOME": str(home)},
    )

    assert result.exit_code != 0
    assert (
        "Remote project suggestions unavailable (older remote); enter paths manually."
        in result.output
    )
    assert "stop after fallback" in result.output
    assert captured["candidates"] == ()

    captured.clear()
    monkeypatch.setattr(
        cli_module,
        "discover_remote_project_candidates",
        lambda target: (_ for _ in ()).throw(ValidationError("malformed response")),
    )
    invalid = CliRunner().invoke(
        main,
        ["sync", "pull", "--ssh", "root@example.com", "--project-env"],
        env={"CODELUX_TEST_HOME": str(home)},
    )
    assert invalid.exit_code != 0
    assert "malformed response" in invalid.output
    assert "suggestions unavailable" not in invalid.output
    assert captured == {}


def test_sync_push_without_selection_prompts_for_contents(tmp_path: Path, monkeypatch) -> None:
    home = _claude_home(tmp_path)
    root = home / ".codelux"
    root.mkdir()
    (root / "providers.json").write_text(
        json.dumps({"schema_version": 1, "providers": {}, "current": {}})
    )
    monkeypatch.setattr(ClaudeAdapter, "is_running", lambda self: ProcessState.NOT_RUNNING)
    monkeypatch.setattr(CodexAdapter, "is_installed", lambda self: False)
    captured = {}

    def succeed(target, manifest, archive, overwrite, progress=None):
        captured["target"] = target
        captured["manifest"] = manifest
        return local_capability(home), {"status": "committed"}

    monkeypatch.setattr(cli_module, "push_archive", succeed)
    result = CliRunner().invoke(
        main,
        ["sync", "push", "--ssh", "root@example.com"],
        input="\nn\n",
        env={"CODELUX_TEST_HOME": str(home)},
    )

    assert result.exit_code == 0, result.output
    assert "Select content to synchronize. Answer each question separately." in result.output
    assert "Press Enter to accept the capitalized default" in result.output
    assert "Providers: third-party endpoints and API keys" in result.output
    assert "(official account logins excluded)? [Y/n]:" in result.output
    assert "Claude Code history: project conversation records? [y/N]:" in result.output
    assert "Codex history: sessions and local session index? [y/N]:" in result.output
    assert captured["target"] == "root@example.com"
    assert captured["manifest"].selection == ("providers",)


def test_project_root_prompt_does_not_default_to_user_home(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("CODELUX_TEST_HOME", str(home))
    monkeypatch.chdir(home)
    prompts = []

    answers = iter([str(project), ""])

    def prompt(message, **kwargs):
        prompts.append((message, kwargs))
        return next(answers)

    monkeypatch.setattr(cli_module.click, "prompt", prompt)

    assert cli_module._environment_project_roots(
        (), local=True, prompt="Source project directory", default_cwd=True
    ) == (project,)
    assert prompts == [
        (
            "Source project directory 1",
            {"default": "", "show_default": False},
        ),
        (
            "Additional source project directory (leave empty to finish)",
            {"default": "", "show_default": False},
        ),
    ]

    prompts.clear()
    monkeypatch.chdir(project)
    answers = iter([str(project), ""])
    assert cli_module._environment_project_roots(
        (), local=True, prompt="Source project directory", default_cwd=True
    ) == (project,)
    assert prompts == [
        (
            "Source project directory 1",
            {"default": str(project), "show_default": True},
        ),
        (
            "Additional source project directory (leave empty to finish)",
            {"default": "", "show_default": False},
        ),
    ]


def test_project_root_prompt_collects_multiple_distinct_projects(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    projects = (tmp_path / "one", tmp_path / "two")
    for project in projects:
        project.mkdir()
    monkeypatch.setenv("CODELUX_TEST_HOME", str(home))
    monkeypatch.chdir(home)
    answers = iter([str(projects[0]), str(projects[1]), ""])
    monkeypatch.setattr(cli_module.click, "prompt", lambda *args, **kwargs: next(answers))

    assert (
        cli_module._environment_project_roots(
            (), local=True, prompt="Source project directory", default_cwd=True
        )
        == projects
    )


def test_project_root_prompt_retries_empty_first_value_and_rejects_duplicates(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("CODELUX_TEST_HOME", str(home))
    monkeypatch.chdir(home)
    answers = iter(["", str(project), ""])
    monkeypatch.setattr(cli_module.click, "prompt", lambda *args, **kwargs: next(answers))

    assert cli_module._environment_project_roots(
        (), local=True, prompt="Source project directory", default_cwd=True
    ) == (project,)

    duplicate_answers = iter([str(project), str(project)])
    monkeypatch.setattr(cli_module.click, "prompt", lambda *args, **kwargs: next(duplicate_answers))
    with pytest.raises(ValidationError, match="must be distinct"):
        cli_module._environment_project_roots(
            (), local=True, prompt="Source project directory", default_cwd=True
        )


def test_project_root_prompt_confirms_session_history_candidates(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    candidates = (tmp_path / "one", tmp_path / "two")
    for candidate in candidates:
        candidate.mkdir()
    monkeypatch.setenv("CODELUX_TEST_HOME", str(home))
    monkeypatch.chdir(home)
    confirmations = iter([True, False])
    questions = []

    def confirm(question, **kwargs):
        questions.append((question, kwargs))
        return next(confirmations)

    monkeypatch.setattr(cli_module.click, "confirm", confirm)
    monkeypatch.setattr(cli_module.click, "prompt", lambda *args, **kwargs: "")

    assert cli_module._environment_project_roots(
        (),
        local=True,
        prompt="Source project directory",
        default_cwd=True,
        candidates=candidates,
    ) == (candidates[0],)
    assert questions == [
        (f"Include suggested source project 1: {candidates[0]}?", {"default": False}),
        (f"Include suggested source project 2: {candidates[1]}?", {"default": False}),
    ]


def test_sync_push_without_selection_can_cancel_all_content(tmp_path: Path, monkeypatch) -> None:
    home = _claude_home(tmp_path)
    monkeypatch.setattr(
        cli_module,
        "push_archive",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unexpected SSH push")),
    )

    result = CliRunner().invoke(
        main,
        ["sync", "push", "--ssh", "root@example.com"],
        input="n\nn\nn\n",
        env={"CODELUX_TEST_HOME": str(home)},
    )

    assert result.exit_code == 0, result.output
    assert "No content selected; synchronization cancelled." in result.output
    assert "Checking source Codelux state" not in result.output


def test_sync_process_preflight_ignores_unselected_running_client(
    tmp_path: Path, monkeypatch
) -> None:
    home = _claude_home(tmp_path)
    (home / ".codex").mkdir()
    root = home / ".codelux"
    root.mkdir()
    (root / "providers.json").write_text(
        json.dumps({"schema_version": 1, "providers": {}, "current": {}})
    )
    monkeypatch.setenv("CODELUX_TEST_HOME", str(home))
    monkeypatch.setattr(ClaudeAdapter, "is_running", lambda self: ProcessState.NOT_RUNNING)
    monkeypatch.setattr(CodexAdapter, "is_running", lambda self: ProcessState.RUNNING)

    cli_module._sync_process_preflight(("sessions",), ("claude",))
    with pytest.raises(ValidationError, match="codex"):
        cli_module._sync_process_preflight(("sessions",))


def test_sync_push_passes_selected_client_to_process_preflight(tmp_path: Path, monkeypatch) -> None:
    home = _claude_home(tmp_path)
    (home / ".codex").mkdir()
    root = home / ".codelux"
    root.mkdir()
    (root / "providers.json").write_text(
        json.dumps({"schema_version": 1, "providers": {}, "current": {}})
    )
    monkeypatch.setattr(ClaudeAdapter, "is_running", lambda self: ProcessState.NOT_RUNNING)
    monkeypatch.setattr(CodexAdapter, "is_running", lambda self: ProcessState.RUNNING)
    monkeypatch.setattr(
        cli_module,
        "push_archive",
        lambda *args, **kwargs: (local_capability(home), {"status": "committed"}),
    )

    result = CliRunner().invoke(
        main,
        ["sync", "push", "--ssh", "root@example.com", "--claude-sessions"],
        input="\n",
        env={"CODELUX_TEST_HOME": str(home)},
    )

    assert result.exit_code == 0, result.output
    assert '"status": "committed"' in result.output


def test_sync_transport_send_passes_selected_client_to_process_preflight(
    tmp_path: Path, monkeypatch
) -> None:
    home = _claude_home(tmp_path)
    (home / ".codex").mkdir()
    root = home / ".codelux"
    root.mkdir()
    (root / "providers.json").write_text(
        json.dumps({"schema_version": 1, "providers": {}, "current": {}})
    )
    monkeypatch.setattr(ClaudeAdapter, "is_running", lambda self: ProcessState.NOT_RUNNING)
    monkeypatch.setattr(CodexAdapter, "is_running", lambda self: ProcessState.RUNNING)

    result = CliRunner().invoke(
        main,
        [
            "sync",
            "transport",
            "send",
            "--protocol",
            "1",
            "--sessions",
            "--claude-sessions",
            "--keys",
        ],
        env={"CODELUX_TEST_HOME": str(home)},
    )

    assert result.exit_code == 0, result.output


def test_sync_push_interactive_sessions_has_no_config_replacement(
    tmp_path: Path, monkeypatch
) -> None:
    home = _claude_home(tmp_path)
    monkeypatch.setattr(ClaudeAdapter, "is_running", lambda self: ProcessState.NOT_RUNNING)
    monkeypatch.setattr(CodexAdapter, "is_installed", lambda self: False)
    captured = {}

    def succeed(target, manifest, archive, overwrite, progress=None):
        captured["manifest"] = manifest
        return local_capability(home), {"status": "committed"}

    monkeypatch.setattr(cli_module, "push_archive", succeed)
    result = CliRunner().invoke(
        main,
        ["sync", "push", "--ssh", "root@example.com"],
        input="n\ny\n",
        env={"CODELUX_TEST_HOME": str(home)},
    )

    assert result.exit_code == 0, result.output
    assert "Replace the target active Provider configuration?" not in result.output
    assert captured["manifest"].selection == ("sessions",)


def test_sync_push_maps_one_of_multiple_claude_projects(tmp_path: Path, monkeypatch) -> None:
    home = _claude_home(tmp_path)
    for slug in ("-source-one", "-source-two"):
        project = home / ".claude/projects" / slug
        project.mkdir(parents=True)
        (project / "session.jsonl").write_text(
            json.dumps({"type": "message", "cwd": f"/{slug}"}) + "\n"
        )
    monkeypatch.setattr(ClaudeAdapter, "is_running", lambda self: ProcessState.NOT_RUNNING)
    monkeypatch.setattr(CodexAdapter, "is_installed", lambda self: False)
    captured = {}

    def succeed(target, manifest, archive, overwrite, progress=None):
        captured["manifest"] = manifest
        captured["archive"] = archive
        return local_capability(home), {"status": "committed"}

    monkeypatch.setattr(cli_module, "push_archive", succeed)
    result = CliRunner().invoke(
        main,
        [
            "sync",
            "push",
            "--ssh",
            "root@example.com",
            "--sessions",
            "--claude-project-root",
            "/target/project",
        ],
        input="1\n",
        env={"CODELUX_TEST_HOME": str(home)},
    )

    assert result.exit_code == 0, result.output
    manifest, files = parse_plain_archive(captured["archive"])
    assert manifest == captured["manifest"]
    assert set(files) == {"claude/projects/-target-project/session.jsonl"}
    assert '"cwd":"/target/project"' in next(iter(files.values())).decode()


def test_sync_push_and_pull_reject_removed_config_options(tmp_path: Path) -> None:
    env = {"CODELUX_TEST_HOME": str(tmp_path)}
    runner = CliRunner()
    cases = [
        ["sync", "push", "--ssh", "root@example.com", "--config"],
        ["sync", "push", "--ssh", "root@example.com", "--apply-active-provider"],
        ["sync", "pull", "--ssh", "root@example.com", "--apply-active-provider"],
    ]
    for args in cases:
        result = runner.invoke(main, args, env=env)
        assert result.exit_code != 0 and "No such option" in result.output


def test_sync_status_reset_and_machine_id_rotate(tmp_path: Path) -> None:
    home = tmp_path / "home"
    root = home / ".codelux"
    root.mkdir(parents=True)
    (root / "machine-id").write_text("a" * 32 + "\n")
    (root / "sync-state.json").write_text(
        '{"schema_version":1,"baselines":{"remote:config":{},"other:sessions":{}}}\n'
    )
    runner = CliRunner()
    env = {"CODELUX_TEST_HOME": str(home)}
    status = runner.invoke(main, ["sync", "status"], env=env)
    reset = runner.invoke(
        main, ["sync", "reset", "--machine", "remote", "--selection", "config"], env=env
    )
    missing = runner.invoke(main, ["sync", "reset", "--machine", "missing"], env=env)
    rotated = runner.invoke(main, ["sync", "machine-id", "rotate"], env=env)

    assert status.exit_code == 0 and "remote:config" in status.output
    assert reset.exit_code == 0 and "baseline reset" in reset.output
    assert missing.exit_code == 0 and "no matching baseline" in missing.output
    assert rotated.exit_code == 0 and "rotated machine-id" in rotated.output
    assert not load_sync_state(root)["baselines"]


def test_sync_transport_endpoints_reject_wrong_protocol(tmp_path: Path) -> None:
    env = {"CODELUX_TEST_HOME": str(tmp_path)}
    runner = CliRunner()
    received = runner.invoke(main, ["sync", "transport", "receive", "--protocol", "2"], env=env)
    sent = runner.invoke(
        main,
        ["sync", "transport", "send", "--protocol", "2", "--providers"],
        env=env,
    )
    discovered = runner.invoke(
        main, ["sync", "transport", "discover-projects", "--protocol", "2"], env=env
    )
    assert received.exit_code != 0 and "unsupported sync protocol" in received.output
    assert sent.exit_code != 0 and "unsupported sync protocol" in sent.output
    assert discovered.exit_code != 0 and "unsupported sync protocol" in discovered.output


def test_sync_transport_discover_projects_outputs_safe_candidates(
    tmp_path: Path, monkeypatch
) -> None:
    home = _claude_home(tmp_path / "home")
    project = tmp_path / "project"
    project.mkdir()
    history = home / ".claude/projects/example"
    history.mkdir(parents=True)
    (history / "session.jsonl").write_text(json.dumps({"cwd": str(project)}) + "\n")
    monkeypatch.setattr(ClaudeAdapter, "is_running", lambda self: ProcessState.NOT_RUNNING)

    result = CliRunner().invoke(
        main,
        ["sync", "transport", "discover-projects", "--protocol", "1"],
        env={"CODELUX_TEST_HOME": str(home)},
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout_bytes) == [str(project)]


def test_sync_pull_applies_remote_provider_archive(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    for home in (source, target):
        (home / ".codelux").mkdir(parents=True)
    source_registry = {
        "schema_version": 1,
        "providers": {
            "proxy": {
                "name": "proxy",
                "description": "",
                "clients": {
                    "claude": {
                        "enabled": True,
                        "base_url": "https://proxy.example",
                        "api_key": "test-secret",
                    }
                },
            }
        },
        "current": {"claude": "proxy"},
    }
    (source / ".codelux/providers.json").write_text(json.dumps(source_registry))
    (target / ".codelux/providers.json").write_text(
        json.dumps({"schema_version": 1, "providers": {}, "current": {}})
    )
    calls = []

    def fake_ssh(command, **kwargs):
        calls.append((command, kwargs))
        remote = CliRunner().invoke(
            main,
            ["sync", "transport", "send", "--protocol", "1", "--providers", "--keys"],
            env={"CODELUX_TEST_HOME": str(source)},
        )
        return subprocess.CompletedProcess(
            command,
            remote.exit_code,
            remote.stdout_bytes,
            remote.stderr_bytes,
        )

    monkeypatch.setattr(sync_transport.subprocess, "run", fake_ssh)

    result = CliRunner().invoke(
        main,
        ["sync", "pull", "--ssh", "root@example.com", "--providers"],
        env={"CODELUX_TEST_HOME": str(target)},
    )

    assert result.exit_code == 0, result.output
    registry = json.loads((target / ".codelux/providers.json").read_bytes())
    assert "proxy" in registry["providers"]
    assert registry["current"] == {}
    assert '"status": "committed"' in result.output
    assert calls[0][0][-7:] == [
        "sync",
        "transport",
        "send",
        "--protocol",
        "1",
        "--providers",
        "--keys",
    ]
    store = SnapshotStore(target / ".codelux")
    operation_id = next(path.name for path in store.backups.iterdir() if path.is_dir())
    assert store.read_manifest(operation_id).operation_type == "sync_pull"


def test_sync_pull_maps_claude_history_to_local_project(tmp_path: Path, monkeypatch) -> None:
    source = _claude_home(tmp_path / "source")
    target = _claude_home(tmp_path / "target")
    source_project = source / ".claude/projects/-root-litellm-custom"
    source_project.mkdir(parents=True)
    (source_project / "session.jsonl").write_text(
        json.dumps({"cwd": "/workspace/example-project"}) + "\n"
    )
    (target / ".codelux").mkdir()
    manifest, paths = build_manifest(source, ["sessions"], clients=("claude",))
    manifest, payload = cli_module.materialize_sync_files(manifest, paths)
    monkeypatch.setattr(cli_module, "_sync_process_preflight", lambda *args: None)
    monkeypatch.setattr(
        cli_module,
        "pull_archive",
        lambda *args, **kwargs: (local_capability(source), manifest, payload),
    )
    local_project = target / "workspace/litellm"
    local_project.mkdir(parents=True)
    result = CliRunner().invoke(
        main,
        ["sync", "pull", "--ssh", "root@example.com", "--claude-sessions", "--overwrite"],
        input=f"{local_project}\n",
        env={"CODELUX_TEST_HOME": str(target)},
    )
    assert result.exit_code == 0, result.output
    mapped = target / ".claude/projects" / _claude_project_slug(local_project)
    assert json.loads((mapped / "session.jsonl").read_text())["cwd"] == str(local_project)


def test_sync_transport_receive_records_push_operation(tmp_path: Path) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    for home in (source, target):
        (home / ".codelux").mkdir(parents=True)
    registry = {"schema_version": 1, "providers": {}, "current": {}}
    (source / ".codelux/providers.json").write_text(json.dumps(registry))
    (target / ".codelux/providers.json").write_text(json.dumps(registry))
    manifest, files = build_manifest(source, ["providers"], include_keys=True)
    archive = create_plain_archive(manifest, files)

    result = CliRunner().invoke(
        main,
        ["sync", "transport", "receive", "--protocol", "1"],
        input=archive,
        env={"CODELUX_TEST_HOME": str(target)},
    )

    assert result.exit_code == 0, result.output
    store = SnapshotStore(target / ".codelux")
    operation_id = next(path.name for path in store.backups.iterdir() if path.is_dir())
    assert store.read_manifest(operation_id).operation_type == "sync_push"


def test_sync_transport_receive_enforces_user_environment_overwrite_scope(
    tmp_path: Path, monkeypatch
) -> None:
    source = _claude_home(tmp_path / "source")
    target = _claude_home(tmp_path / "target")
    for home in (source, target):
        (home / ".codelux").mkdir()
        (home / ".codelux/providers.json").write_text(
            json.dumps({"schema_version": 1, "providers": {}, "current": {}})
        )
    (source / ".claude/settings.json").write_text('{"source":true}\n')
    (target / ".claude/settings.json").write_text('{"target":true}\n')
    manifest, files = build_manifest(source, ["user_env"])
    manifest, payload = materialize_sync_files(manifest, files)
    archive = create_plain_archive(manifest, tuple(payload.items()))
    monkeypatch.setattr(ClaudeAdapter, "is_running", lambda self: ProcessState.NOT_RUNNING)
    monkeypatch.setattr(CodexAdapter, "is_running", lambda self: ProcessState.NOT_RUNNING)
    runner = CliRunner()

    rejected = runner.invoke(
        main,
        ["sync", "transport", "receive", "--protocol", "1"],
        input=archive,
        env={"CODELUX_TEST_HOME": str(target)},
    )
    assert rejected.exit_code != 0
    assert (
        "sync conflicts were not approved for overwrite: user-env/claude/settings.json"
        in rejected.output
    )
    assert (target / ".claude/settings.json").read_text() == '{"target":true}\n'

    accepted = runner.invoke(
        main,
        [
            "sync",
            "transport",
            "receive",
            "--protocol",
            "1",
            "--overwrite-scope",
            "user_env",
        ],
        input=archive,
        env={"CODELUX_TEST_HOME": str(target)},
    )
    assert accepted.exit_code == 0, accepted.output
    assert json.loads((target / ".claude/settings.json").read_text()) == {"source": True}


def test_sync_transport_receive_checks_selected_target_process(tmp_path: Path, monkeypatch) -> None:
    source = _claude_home(tmp_path / "source")
    project = source / ".claude/projects/source"
    project.mkdir(parents=True)
    (project / "session.jsonl").write_text('{"cwd":"/source/project"}\n')
    target = _claude_home(tmp_path / "target")
    (target / ".codelux").mkdir()
    (target / ".codelux/providers.json").write_text(
        json.dumps({"schema_version": 1, "providers": {}, "current": {}})
    )
    manifest, files = build_manifest(source, ["sessions"], clients=("claude",))
    archive = create_plain_archive(manifest, files)
    monkeypatch.setattr(ClaudeAdapter, "is_running", lambda self: ProcessState.RUNNING)

    result = CliRunner().invoke(
        main,
        ["sync", "transport", "receive", "--protocol", "1"],
        input=archive,
        env={"CODELUX_TEST_HOME": str(target)},
    )

    assert result.exit_code != 0
    assert "sync clients are running or process state is unknown: claude" in result.output
    assert not (target / ".claude/projects/source/session.jsonl").exists()


def test_sync_transport_receive_requires_existing_real_target_project(
    tmp_path: Path, monkeypatch
) -> None:
    source = _claude_home(tmp_path / "source")
    project = source / ".claude/projects/source"
    project.mkdir(parents=True)
    missing_project = tmp_path / "missing-project"
    (project / "session.jsonl").write_text(json.dumps({"cwd": str(missing_project)}) + "\n")
    target = _claude_home(tmp_path / "target")
    (target / ".codelux").mkdir()
    (target / ".codelux/providers.json").write_text(
        json.dumps({"schema_version": 1, "providers": {}, "current": {}})
    )
    manifest, files = build_manifest(source, ["sessions"], clients=("claude",))
    archive = create_plain_archive(manifest, files)
    monkeypatch.setattr(ClaudeAdapter, "is_running", lambda self: ProcessState.NOT_RUNNING)

    result = CliRunner().invoke(
        main,
        ["sync", "transport", "receive", "--protocol", "1"],
        input=archive,
        env={"CODELUX_TEST_HOME": str(target)},
    )

    assert result.exit_code != 0
    assert "local project directory does not exist" in result.output
    assert not (target / ".claude/projects/source/session.jsonl").exists()


def test_sync_pull_rejects_removed_config_option(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        main,
        ["sync", "pull", "--ssh", "root@example.com", "--config"],
        env={"CODELUX_TEST_HOME": str(tmp_path)},
    )

    assert result.exit_code != 0
    assert "No such option" in result.output and "--config" in result.output


def test_sync_source_standardization_merges_same_provider_client_bindings(
    tmp_path: Path, monkeypatch
) -> None:
    home = _claude_home(tmp_path)
    (home / ".claude/settings.json").write_text(
        json.dumps(
            {
                "env": {
                    "ANTHROPIC_BASE_URL": "https://proxy.example",
                    "ANTHROPIC_AUTH_TOKEN": "shared-secret",
                }
            }
        )
    )
    codex = home / ".codex"
    codex.mkdir()
    (codex / "config.toml").write_text(
        'model_provider = "custom"\n'
        "[model_providers.custom]\n"
        'base_url = "https://proxy.example"\n'
        'wire_api = "responses"\n'
        "requires_openai_auth = true\n"
    )
    (codex / "auth.json").write_text(
        json.dumps({"auth_mode": "apikey", "OPENAI_API_KEY": "shared-secret"})
    )
    registry_root = home / ".codelux"
    registry_root.mkdir()
    (registry_root / "providers.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "providers": {
                    "custom": {
                        "name": "custom",
                        "description": "",
                        "clients": {
                            "codex": {
                                "enabled": True,
                                "base_url": "https://proxy.example",
                                "api_key": "shared-secret",
                                "wire_api": "responses",
                                "requires_openai_auth": True,
                            }
                        },
                    }
                },
                "current": {"codex": "custom"},
            }
        )
    )
    monkeypatch.setenv("CODELUX_TEST_HOME", str(home))

    cli_module._standardize_sync_source(("providers",))

    registry = json.loads((registry_root / "providers.json").read_text())
    assert set(registry["providers"]["custom"]["clients"]) == {"claude", "codex"}
    assert registry["current"] == {"claude": "custom", "codex": "custom"}


def test_explicit_switch_repairs_unregistered_active_codex_provider(
    tmp_path: Path, monkeypatch
) -> None:
    codex = tmp_path / ".codex"
    codex.mkdir()
    (codex / "config.toml").write_text(
        'model_provider = "legacy"\n'
        "[model_providers.legacy]\n"
        'base_url = "https://legacy.example"\n'
        'wire_api = "responses"\n'
        "requires_openai_auth = true\n"
    )
    (codex / "auth.json").write_text(
        json.dumps({"auth_mode": "apikey", "OPENAI_API_KEY": "legacy-secret"})
    )
    registry_root = tmp_path / ".codelux"
    registry_root.mkdir()
    (registry_root / "providers.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "providers": {
                    "custom": {
                        "name": "custom",
                        "description": "",
                        "clients": {
                            "codex": {
                                "enabled": True,
                                "base_url": "https://proxy.example",
                                "api_key": "new-secret",
                                "wire_api": "responses",
                                "requires_openai_auth": True,
                            }
                        },
                    }
                },
                "current": {"codex": "custom"},
            }
        )
    )
    monkeypatch.setattr(CodexAdapter, "is_running", lambda self: ProcessState.NOT_RUNNING)

    result = CliRunner().invoke(
        main,
        ["switch", "custom", "--client", "codex"],
        env={"CODELUX_TEST_HOME": str(tmp_path)},
    )

    assert result.exit_code == 0, result.output
    assert 'model_provider = "custom"' in (codex / "config.toml").read_text()


def test_add_accepts_client_environment_key(tmp_path: Path, monkeypatch) -> None:
    home = _claude_home(tmp_path)
    monkeypatch.setattr(ClaudeAdapter, "is_running", lambda self: ProcessState.NOT_RUNNING)
    monkeypatch.setenv("CODELUX_CLAUDE_API_KEY", "env-secret")
    result = CliRunner().invoke(
        main,
        ["add", "proxy", "--url", "https://proxy.example", "--client", "claude"],
        env={"CODELUX_TEST_HOME": str(home)},
    )
    assert result.exit_code == 0, result.output
    assert "env-secret" not in result.output


def test_add_accepts_single_client_key_stdin(tmp_path: Path, monkeypatch) -> None:
    home = _claude_home(tmp_path)
    monkeypatch.setattr(ClaudeAdapter, "is_running", lambda self: ProcessState.NOT_RUNNING)
    result = CliRunner().invoke(
        main,
        ["add", "proxy", "--url", "https://proxy.example", "--client", "claude", "--key-stdin"],
        input="stdin-secret\n",
        env={"CODELUX_TEST_HOME": str(home)},
    )
    assert result.exit_code == 0, result.output
    assert "stdin-secret" not in result.output


def test_add_adopts_exact_claude_configuration_without_rewriting(
    tmp_path: Path, monkeypatch
) -> None:
    home = _claude_home(tmp_path)
    settings = home / ".claude" / "settings.json"
    settings.write_text(
        '{"keep":true,"env":{"ANTHROPIC_AUTH_TOKEN":"legacy-secret",'
        '"ANTHROPIC_BASE_URL":"https://legacy.example"}}\n'
    )
    before = settings.read_bytes()
    monkeypatch.setattr(ClaudeAdapter, "is_running", lambda self: ProcessState.NOT_RUNNING)

    result = CliRunner().invoke(
        main,
        ["add", "legacy", "--url", "https://legacy.example", "--client", "claude"],
        input="legacy-secret\nlegacy-secret\n",
        env={"CODELUX_TEST_HOME": str(home)},
    )

    assert result.exit_code == 0, result.output
    assert "adopted" in result.output
    assert settings.read_bytes() == before
    assert cli_module.load_registry(home / ".codelux").desired["claude"] == "legacy"


def test_add_rejects_unknown_process_state_without_writing(tmp_path: Path, monkeypatch) -> None:
    home = _claude_home(tmp_path)
    settings = home / ".claude" / "settings.json"
    before = settings.read_bytes()
    monkeypatch.setattr(ClaudeAdapter, "is_running", lambda self: ProcessState.UNKNOWN)

    result = CliRunner().invoke(
        main,
        ["add", "proxy", "--url", "https://proxy.example", "--client", "claude"],
        env={"CODELUX_TEST_HOME": str(home), "CODELUX_CLAUDE_API_KEY": "secret"},
    )

    assert result.exit_code != 0
    assert "unknown" in result.output
    assert settings.read_bytes() == before
    assert not (home / ".codelux" / "providers.json").exists()


def test_update_writes_active_provider_and_requires_restart(tmp_path: Path, monkeypatch) -> None:
    home = _claude_home(tmp_path)
    monkeypatch.setattr(ClaudeAdapter, "is_running", lambda self: ProcessState.NOT_RUNNING)
    env = {"CODELUX_TEST_HOME": str(home)}
    runner = CliRunner()
    added = runner.invoke(
        main,
        ["add", "proxy", "--url", "https://proxy.example", "--client", "claude"],
        input="old-secret\nold-secret\n",
        env=env,
    )
    assert added.exit_code == 0, added.output
    monkeypatch.setattr(ClaudeAdapter, "is_running", lambda self: ProcessState.RUNNING)

    updated = runner.invoke(
        main,
        ["update", "proxy", "--client", "claude"],
        input="https://new.example\nnew-secret\nnew-secret\n",
        env=env,
    )

    assert updated.exit_code == 0, updated.output
    assert "restart claude" in updated.output
    binding = cli_module.load_registry(home / ".codelux").providers["proxy"].clients["claude"]
    assert binding.base_url == "https://new.example"
    assert binding.api_key == "new-secret"
    settings = json.loads((home / ".claude" / "settings.json").read_text())
    assert settings["env"]["ANTHROPIC_BASE_URL"] == "https://new.example"
    assert settings["env"]["ANTHROPIC_AUTH_TOKEN"] == "new-secret"


def test_status_ignores_legacy_config_and_uses_explicit_format(tmp_path: Path) -> None:
    home = _claude_home(tmp_path)
    root = home / ".codelux"
    root.mkdir()
    (root / "config.json").write_text('{"default_format":"json"}\n')
    result = CliRunner().invoke(
        main,
        ["status", "--client", "claude", "--format", "json"],
        env={"CODELUX_TEST_HOME": str(home)},
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)[0]["client"] == "claude"


def test_list_ignores_legacy_config_and_uses_explicit_format(tmp_path: Path) -> None:
    home = _claude_home(tmp_path)
    root = home / ".codelux"
    root.mkdir()
    (root / "config.json").write_text('{"default_format":"json"}\n')
    result = CliRunner().invoke(
        main, ["list", "--format", "json"], env={"CODELUX_TEST_HOME": str(home)}
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == [
        {
            "name": "official",
            "clients": ["claude", "codex"],
            "description": "Built-in official Provider",
            "builtin": True,
        }
    ]


def test_remove_force_cannot_remove_active_provider(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(ClaudeAdapter, "is_running", lambda self: ProcessState.NOT_RUNNING)
    home = _claude_home(tmp_path)
    runner = CliRunner()
    env = {"CODELUX_TEST_HOME": str(home)}
    runner.invoke(
        main,
        ["add", "proxy", "--url", "https://proxy.example", "--client", "claude"],
        input="test-secret\ntest-secret\n",
        env=env,
    )
    runner.invoke(main, ["switch", "proxy", "--client", "claude"], env=env)

    removed = runner.invoke(main, ["remove", "proxy", "--client", "claude", "--force"], env=env)
    assert removed.exit_code != 0
    assert "active" in removed.output


def test_registry_commit_failure_rolls_back_client(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(ClaudeAdapter, "is_running", lambda self: ProcessState.NOT_RUNNING)
    home = _claude_home(tmp_path)
    runner = CliRunner()
    env = {"CODELUX_TEST_HOME": str(home)}

    def fail_registry(root: Path, registry) -> None:
        raise OSError("injected registry failure")

    monkeypatch.setattr(cli_module, "_save_registry", fail_registry)
    added = runner.invoke(
        main,
        ["add", "proxy", "--url", "https://proxy.example", "--client", "claude"],
        input="test-secret\ntest-secret\n",
        env=env,
    )
    assert added.exit_code != 0
    assert "rolled back" in added.output
    settings = json.loads((home / ".claude" / "settings.json").read_text())
    assert settings["env"] == {}


def test_official_restore_registry_failure_rolls_back_custom_state(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(ClaudeAdapter, "is_running", lambda self: ProcessState.NOT_RUNNING)
    home = _claude_home(tmp_path)
    runner = CliRunner()
    env = {"CODELUX_TEST_HOME": str(home)}
    assert (
        runner.invoke(
            main,
            ["add", "proxy", "--url", "https://proxy.example", "--client", "claude"],
            input="test-secret\ntest-secret\n",
            env=env,
        ).exit_code
        == 0
    )
    assert runner.invoke(main, ["switch", "proxy", "--client", "claude"], env=env).exit_code == 0
    custom = (home / ".claude" / "settings.json").read_bytes()

    monkeypatch.setattr(
        cli_module,
        "_save_registry",
        lambda root, registry: (_ for _ in ()).throw(OSError("injected registry failure")),
    )
    restored = runner.invoke(main, ["switch", "official", "--client", "claude"], env=env)

    assert restored.exit_code != 0
    assert "rolled back" in restored.output
    assert (home / ".claude" / "settings.json").read_bytes() == custom


def test_remove_rejects_registry_drift_and_requires_force_for_history(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(ClaudeAdapter, "is_running", lambda self: ProcessState.NOT_RUNNING)
    home = _claude_home(tmp_path)
    runner = CliRunner()
    env = {"CODELUX_TEST_HOME": str(home)}
    assert (
        runner.invoke(
            main,
            ["add", "proxy", "--url", "https://proxy.example", "--client", "claude"],
            input="test-secret\ntest-secret\n",
            env=env,
        ).exit_code
        == 0
    )
    assert runner.invoke(main, ["switch", "proxy", "--client", "claude"], env=env).exit_code == 0

    registry_path = home / ".codelux" / "providers.json"
    registry = json.loads(registry_path.read_text())
    registry["current"]["claude"] = None
    registry_path.write_text(json.dumps(registry))
    drifted = runner.invoke(main, ["remove", "proxy", "--client", "claude"], env=env)
    assert drifted.exit_code != 0
    assert "drifted" in drifted.output

    registry["current"]["claude"] = "proxy"
    registry_path.write_text(json.dumps(registry))
    active = runner.invoke(main, ["remove", "proxy", "--client", "claude", "--force"], env=env)
    assert active.exit_code != 0
    assert "active" in active.output


def test_snapshot_failure_leaves_client_unchanged(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(ClaudeAdapter, "is_running", lambda self: ProcessState.NOT_RUNNING)
    home = _claude_home(tmp_path)
    runner = CliRunner()
    env = {"CODELUX_TEST_HOME": str(home)}

    def fail_snapshot(*args, **kwargs):
        raise ValidationError("injected snapshot failure")

    monkeypatch.setattr(cli_module.SnapshotStore, "create", fail_snapshot)
    added = runner.invoke(
        main,
        ["add", "proxy", "--url", "https://proxy.example", "--client", "claude"],
        input="test-secret\ntest-secret\n",
        env=env,
    )
    assert added.exit_code != 0
    settings = json.loads((home / ".claude" / "settings.json").read_text())
    assert settings["env"] == {}


def test_status_reports_registry_drift(tmp_path: Path, monkeypatch) -> None:
    home = _claude_home(tmp_path)
    monkeypatch.setattr(ClaudeAdapter, "is_running", lambda self: ProcessState.NOT_RUNNING)
    runner = CliRunner()
    env = {"CODELUX_TEST_HOME": str(home)}
    added = runner.invoke(
        main,
        ["add", "proxy", "--url", "https://proxy.example", "--client", "claude"],
        input="test-secret\ntest-secret\n",
        env=env,
    )
    assert added.exit_code == 0
    (home / ".claude" / "settings.json").write_text('{"env": {}}\n')
    result = runner.invoke(main, ["status", "--client", "claude", "--format", "json"], env=env)
    assert result.exit_code == 0
    assert json.loads(result.output)[0]["health"] == "drifted"
