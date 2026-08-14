import json
from pathlib import Path

from click.testing import CliRunner

from codelux.adapters.claude import ClaudeAdapter
from codelux.adapters.codex import CodexAdapter
from codelux.cli import main
from codelux.models import (
    ConfigFile,
    ConfigState,
    FileState,
    ObservedConfig,
    OperationState,
    PreparedChange,
    ProcessState,
)
from codelux.registry_io import load_registry
from codelux.snapshots import SnapshotStore


def _dual_home(tmp_path: Path) -> Path:
    claude = tmp_path / ".claude"
    claude.mkdir()
    (claude / "settings.json").write_text('{"env": {}, "keep": true}\n')
    codex = tmp_path / ".codex"
    codex.mkdir()
    (codex / "config.toml").write_text(
        'model_provider = "openai"\n\n'
        "[model_providers.proxy]\n"
        'name = "Proxy"\n'
        'base_url = "https://old.invalid/v1"\n'
        'wire_api = "responses"\n'
        "requires_openai_auth = true\n"
        "custom_timeout = 60\n"
    )
    (codex / "auth.json").write_text(
        json.dumps({"auth_mode": "chatgpt", "tokens": {"access_token": "official-token"}})
    )
    return tmp_path


def _not_running(monkeypatch) -> None:
    monkeypatch.setattr(ClaudeAdapter, "is_running", lambda self: ProcessState.NOT_RUNNING)
    monkeypatch.setattr(CodexAdapter, "is_running", lambda self: ProcessState.NOT_RUNNING)


def _add_dual_provider(runner: CliRunner, env: dict[str, str]):
    claude = runner.invoke(
        main,
        ["add", "proxy", "--url", "https://proxy.invalid/v1", "--client", "claude"],
        input="claude-secret\nclaude-secret\n",
        env=env,
    )
    if claude.exit_code != 0:
        return claude
    return runner.invoke(
        main,
        ["add", "proxy", "--url", "https://proxy.invalid/v1", "--client", "codex"],
        input="codex-secret\ncodex-secret\n",
        env=env,
    )


def test_clients_activate_and_restore_independently(tmp_path: Path, monkeypatch) -> None:
    _not_running(monkeypatch)
    home = _dual_home(tmp_path)
    env = {"CODELUX_TEST_HOME": str(home)}
    runner = CliRunner()

    added = _add_dual_provider(runner, env)
    assert added.exit_code == 0, added.output
    assert json.loads((home / ".claude" / "settings.json").read_text())["keep"] is True
    assert 'model_provider = "custom"' in (home / ".codex" / "config.toml").read_text()
    assert load_registry(home / ".codelux").current == {
        "claude": "proxy",
        "codex": "proxy",
    }

    restored_claude = runner.invoke(main, ["switch", "official", "--client", "claude"], env=env)
    assert restored_claude.exit_code == 0, restored_claude.output
    restored_codex = runner.invoke(main, ["switch", "official", "--client", "codex"], env=env)
    assert restored_codex.exit_code == 0, restored_codex.output
    assert json.loads((home / ".claude" / "settings.json").read_text())["env"] == {}
    restored_config = (home / ".codex" / "config.toml").read_text()
    assert 'model_provider = "custom"' in restored_config
    assert 'name = "OpenAI"' in restored_config
    assert "base_url" not in restored_config.split("[model_providers.custom]", 1)[1]
    assert json.loads((home / ".codex" / "auth.json").read_text())["auth_mode"] == "chatgpt"
    assert load_registry(home / ".codelux").current == {"claude": None, "codex": None}


def test_switch_no_shared_session_preserves_codex_history(tmp_path: Path, monkeypatch) -> None:
    _not_running(monkeypatch)
    home = _dual_home(tmp_path)
    env = {"CODELUX_TEST_HOME": str(home)}
    runner = CliRunner()
    assert _add_dual_provider(runner, env).exit_code == 0
    session_root = home / ".codex" / "sessions" / "2026" / "08" / "08"
    session_root.mkdir(parents=True)
    session = session_root / "session.jsonl"
    session.write_text(
        json.dumps({"type": "session_meta", "payload": {"id": "sid", "model_provider": "openai"}})
        + "\n"
    )
    before = session.read_bytes()
    switched = runner.invoke(
        main,
        ["switch", "proxy", "--client", "codex", "--no-shared-session"],
        env=env,
    )
    assert switched.exit_code == 0, switched.output
    assert session.read_bytes() == before


def test_switch_shares_codex_session_by_default(tmp_path: Path, monkeypatch) -> None:
    _not_running(monkeypatch)
    home = _dual_home(tmp_path)
    env = {"CODELUX_TEST_HOME": str(home)}
    runner = CliRunner()
    assert _add_dual_provider(runner, env).exit_code == 0
    session_root = home / ".codex" / "sessions" / "2026" / "08" / "08"
    session_root.mkdir(parents=True)
    session = session_root / "session.jsonl"
    session.write_text(
        json.dumps({"type": "session_meta", "payload": {"id": "sid", "model_provider": "openai"}})
        + "\n"
    )
    switched = runner.invoke(main, ["switch", "proxy", "--client", "codex"], env=env)
    assert switched.exit_code == 0, switched.output
    assert json.loads(session.read_text())["payload"]["model_provider"] == "custom"


def test_single_client_failure_rolls_back_and_can_retry(tmp_path: Path, monkeypatch) -> None:
    _not_running(monkeypatch)
    home = _dual_home(tmp_path)
    env = {"CODELUX_TEST_HOME": str(home)}
    runner = CliRunner()
    assert _add_dual_provider(runner, env).exit_code == 0
    original_commit = CodexAdapter.commit

    def fail_commit(self, change) -> None:
        raise OSError("injected Codex commit failure")

    monkeypatch.setattr(CodexAdapter, "commit", fail_commit)
    failed = runner.invoke(main, ["switch", "official", "--client", "codex"], env=env)
    assert failed.exit_code != 0
    assert 'model_provider = "custom"' in (home / ".codex" / "config.toml").read_text()
    assert load_registry(home / ".codelux").current["codex"] == "proxy"

    monkeypatch.setattr(CodexAdapter, "commit", original_commit)
    retried = runner.invoke(main, ["switch", "official", "--client", "codex"], env=env)
    assert retried.exit_code == 0, retried.output


def test_explicit_switch_repairs_registry_drift(tmp_path: Path, monkeypatch) -> None:
    _not_running(monkeypatch)
    home = _dual_home(tmp_path)
    env = {"CODELUX_TEST_HOME": str(home)}
    runner = CliRunner()
    assert _add_dual_provider(runner, env).exit_code == 0
    settings_path = home / ".claude" / "settings.json"
    settings_path.write_text(
        json.dumps(
            {
                "env": {
                    "ANTHROPIC_BASE_URL": "https://proxy.invalid/v1",
                    "ANTHROPIC_AUTH_TOKEN": "claude-secret",
                },
                "keep": True,
            }
        )
    )
    registry_path = home / ".codelux" / "providers.json"
    registry = json.loads(registry_path.read_text())
    registry["current"]["claude"] = None
    registry_path.write_text(json.dumps(registry))

    repaired = runner.invoke(main, ["switch", "proxy", "--client", "claude"], env=env)
    assert repaired.exit_code == 0, repaired.output
    assert load_registry(home / ".codelux").current["claude"] == "proxy"


def test_recovery_record_blocks_other_writes(tmp_path: Path) -> None:
    home = _dual_home(tmp_path)
    root = home / ".codelux"
    root.mkdir()
    (root / "recovery.json").write_text("{}")
    env = {"CODELUX_TEST_HOME": str(home)}

    result = CliRunner().invoke(main, ["sync", "machine-id", "rotate"], env=env)
    assert result.exit_code != 0
    assert "recovery is required" in result.output


def test_recover_dry_run_then_restore(tmp_path: Path, monkeypatch) -> None:
    _not_running(monkeypatch)
    home = _dual_home(tmp_path)
    settings = home / ".claude" / "settings.json"
    official = settings.read_bytes()
    custom = b'{"env":{"ANTHROPIC_BASE_URL":"https://broken.invalid"}}\n'
    settings.write_bytes(custom)
    change = PreparedChange(
        "claude",
        (ConfigFile(settings, official, 0o600),),
        (ConfigFile(settings, custom, 0o600),),
        ObservedConfig(ConfigState.OFFICIAL_LOGIN, None, None, None),
    )
    store = SnapshotStore(home / ".codelux")
    manifest = store.create((change,), "switch", "proxy", {"claude": None})
    manifest = store.update_client_state(
        manifest,
        "claude",
        FileState.RECOVERY_REQUIRED,
        OperationState.RECOVERY_REQUIRED,
    )
    store.write_recovery(manifest)
    env = {"CODELUX_TEST_HOME": str(home)}
    runner = CliRunner()

    preview = runner.invoke(main, ["recover", "--dry-run"], env=env)
    assert preview.exit_code == 0, preview.output
    assert manifest.operation_id in preview.output
    assert settings.read_bytes() == custom
    assert (store.root / "recovery.json").is_file()

    recovered = runner.invoke(main, ["recover"], env=env)
    assert recovered.exit_code == 0, recovered.output
    assert settings.read_bytes() == official
    assert not (store.root / "recovery.json").exists()
    assert store.read_manifest(manifest.operation_id).state is OperationState.ROLLED_BACK


def test_switch_rejects_external_override_without_writing(tmp_path: Path, monkeypatch) -> None:
    _not_running(monkeypatch)
    home = _dual_home(tmp_path)
    env = {"CODELUX_TEST_HOME": str(home)}
    runner = CliRunner()
    assert _add_dual_provider(runner, env).exit_code == 0
    before = (home / ".claude" / "settings.json").read_bytes()

    override_env = dict(env, ANTHROPIC_BASE_URL="https://external.invalid")
    result = runner.invoke(main, ["switch", "proxy", "--client", "claude"], env=override_env)
    assert result.exit_code != 0
    assert "external_override" in result.output
    assert (home / ".claude" / "settings.json").read_bytes() == before


def test_switch_rejects_running_client_without_writing(tmp_path: Path, monkeypatch) -> None:
    home = _dual_home(tmp_path)
    env = {"CODELUX_TEST_HOME": str(home)}
    runner = CliRunner()
    _not_running(monkeypatch)
    assert _add_dual_provider(runner, env).exit_code == 0
    monkeypatch.setattr(ClaudeAdapter, "is_running", lambda self: ProcessState.RUNNING)
    before = (home / ".claude" / "settings.json").read_bytes()

    result = runner.invoke(main, ["switch", "proxy", "--client", "claude"], env=env)
    assert result.exit_code != 0
    assert "running" in result.output
    assert (home / ".claude" / "settings.json").read_bytes() == before


def test_status_and_recover_diagnose_orphaned_prepared_operation(
    tmp_path: Path, monkeypatch
) -> None:
    _not_running(monkeypatch)
    home = _dual_home(tmp_path)
    settings = home / ".claude" / "settings.json"
    official = settings.read_bytes()
    custom = b'{"env":{"ANTHROPIC_BASE_URL":"https://partial.invalid"}}\n'
    change = PreparedChange(
        "claude",
        (ConfigFile(settings, official, 0o600),),
        (ConfigFile(settings, custom, 0o600),),
        ObservedConfig(ConfigState.OFFICIAL_LOGIN, None, None, None),
    )
    store = SnapshotStore(home / ".codelux")
    manifest = store.create((change,), "switch", "proxy", {"claude": None})
    settings.write_bytes(custom)
    env = {"CODELUX_TEST_HOME": str(home)}
    runner = CliRunner()

    status = runner.invoke(main, ["status", "--client", "claude", "--format", "json"], env=env)
    assert status.exit_code == 0, status.output
    row = json.loads(status.output)[0]
    assert row["health"] == "recovery_required"
    assert manifest.operation_id in " ".join(row["reasons"])

    recovered = runner.invoke(main, ["recover"], env=env)
    assert recovered.exit_code == 0, recovered.output
    assert settings.read_bytes() == official
    assert store.read_manifest(manifest.operation_id).state is OperationState.ROLLED_BACK


def test_update_and_remove_each_client_binding(tmp_path: Path, monkeypatch) -> None:
    _not_running(monkeypatch)
    home = _dual_home(tmp_path)
    env = {"CODELUX_TEST_HOME": str(home)}
    runner = CliRunner()
    assert _add_dual_provider(runner, env).exit_code == 0

    assert runner.invoke(main, ["switch", "official", "--client", "claude"], env=env).exit_code == 0
    assert runner.invoke(main, ["switch", "official", "--client", "codex"], env=env).exit_code == 0

    updated_claude = runner.invoke(
        main,
        [
            "update",
            "proxy",
            "--client",
            "claude",
            "--url",
            "https://new-claude.example",
        ],
        input="new-claude\nnew-claude\n",
        env=env,
    )
    assert updated_claude.exit_code == 0, updated_claude.output
    updated_codex = runner.invoke(
        main,
        [
            "update",
            "proxy",
            "--client",
            "codex",
            "--url",
            "https://new-codex.example",
        ],
        input="new-codex\nnew-codex\n",
        env=env,
    )
    assert updated_codex.exit_code == 0, updated_codex.output
    provider = load_registry(home / ".codelux").providers["proxy"]
    assert provider.clients["claude"].api_key == "new-claude"
    assert provider.clients["codex"].api_key == "new-codex"
    assert provider.clients["claude"].base_url == "https://new-claude.example"
    assert provider.clients["codex"].base_url == "https://new-codex.example"
    assert "new-claude" not in updated_claude.output
    assert "new-codex" not in updated_codex.output

    removed_claude = runner.invoke(
        main, ["remove", "proxy", "--client", "claude", "--force"], env=env
    )
    assert removed_claude.exit_code == 0, removed_claude.output
    assert set(load_registry(home / ".codelux").providers["proxy"].clients) == {"codex"}
    removed_codex = runner.invoke(
        main, ["remove", "proxy", "--client", "codex", "--force"], env=env
    )
    assert removed_codex.exit_code == 0, removed_codex.output
    assert "proxy" not in load_registry(home / ".codelux").providers


def test_remove_without_client_removes_all_inactive_bindings(tmp_path: Path, monkeypatch) -> None:
    _not_running(monkeypatch)
    home = _dual_home(tmp_path)
    env = {"CODELUX_TEST_HOME": str(home)}
    runner = CliRunner()
    assert _add_dual_provider(runner, env).exit_code == 0
    assert runner.invoke(main, ["switch", "official", "--client", "claude"], env=env).exit_code == 0
    assert runner.invoke(main, ["switch", "official", "--client", "codex"], env=env).exit_code == 0

    removed = runner.invoke(main, ["remove", "proxy", "--force"], env=env)

    assert removed.exit_code == 0, removed.output
    assert "proxy" not in load_registry(home / ".codelux").providers


def test_unsupported_registry_schema_allows_diagnosis_but_blocks_writes(tmp_path: Path) -> None:
    home = _dual_home(tmp_path)
    root = home / ".codelux"
    root.mkdir()
    registry = root / "providers.json"
    registry.write_text('{"schema_version":99,"providers":{},"current":{}}')
    before = registry.read_bytes()
    env = {"CODELUX_TEST_HOME": str(home)}
    runner = CliRunner()

    diagnosed = runner.invoke(main, ["status", "--client", "claude"], env=env)
    assert diagnosed.exit_code != 0
    assert "registry is invalid" in diagnosed.output
    blocked = runner.invoke(main, ["update", "missing", "--client", "claude"], env=env)
    assert blocked.exit_code != 0
    assert "registry is invalid" in blocked.output
    assert registry.read_bytes() == before
