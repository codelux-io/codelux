import json
import subprocess
from pathlib import Path

import pytest

import codelux.adapters.codex as codex_module
from codelux.adapters.codex import CodexAdapter
from codelux.errors import RecoveryRequiredError, ValidationError
from codelux.models import ConfigState, ProcessState
from codelux.registry import ClientBinding, ProviderRecord, Registry


def _registry() -> Registry:
    return Registry(
        providers={
            "proxy": ProviderRecord(
                "proxy",
                {"codex": ClientBinding("https://new.example", "new-secret", "responses", True)},
            )
        },
        current={"codex": None},
    )


def test_process_check_ignores_client_name_in_arguments(monkeypatch, tmp_path: Path) -> None:
    adapter = CodexAdapter(tmp_path, _registry())

    class Result:
        stdout = b"123 codelux switch proxy --client codex\n"

    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: Result())
    assert adapter.is_running() is ProcessState.NOT_RUNNING


def test_process_check_detects_node_and_vendor_codex(monkeypatch, tmp_path: Path) -> None:
    adapter = CodexAdapter(tmp_path, _registry())

    class Result:
        stdout = b"23522 node /Users/test/.nvm/bin/codex\n" b"23523 /Users/test/vendor/bin/codex\n"

    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: Result())
    assert adapter.is_running() is ProcessState.RUNNING


def test_missing_provider_table_is_created_preserving_existing_content(tmp_path: Path) -> None:
    home = tmp_path
    codex = home / ".codex"
    codex.mkdir()
    (codex / "config.toml").write_text(
        '# keep this comment\nmodel_provider = "openai"\nunknown = "keep"\n'
    )
    (codex / "auth.json").write_text(json.dumps({"auth_mode": "apikey", "OPENAI_API_KEY": "old"}))
    from codelux.adapters.codex import CodexAdapter
    from codelux.registry import ClientBinding

    change = CodexAdapter(home).prepare_provider(
        ClientBinding("https://proxy.example", "new", "responses", True).to_dict()
        | {"provider_id": "proxy"}
    )
    text = next(
        file.content for file in change.after if file.path == codex / "config.toml"
    ).decode()
    assert "# keep this comment\n" in text
    assert "[model_providers.proxy]" in text


def test_codex_custom_patch_preserves_unowned_text(tmp_path: Path) -> None:
    config_dir = tmp_path / ".codex"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text(
        '# keep this comment\nmodel_provider = "proxy"\n\n'
        "[model_providers.proxy]\n"
        'name = "Proxy"\n'
        'base_url = "https://old.example"\n'
        'wire_api = "responses"\n'
        "requires_openai_auth = true\n"
        "custom_timeout = 60\n"
    )
    (config_dir / "auth.json").write_text(
        json.dumps({"OPENAI_API_KEY": "old-secret", "auth_mode": "apikey"})
    )
    adapter = CodexAdapter(tmp_path, _registry())
    change = adapter.prepare_provider(
        {
            "provider_id": "proxy",
            "base_url": "https://new.example",
            "api_key": "new-secret",
            "wire_api": "responses",
            "requires_openai_auth": True,
        }
    )
    adapter.commit(change)
    text = (config_dir / "config.toml").read_text()
    assert "# keep this comment" in text and "custom_timeout = 60" in text
    assert adapter.inspect().state is ConfigState.CUSTOM
    adapter.rollback(change)
    assert adapter.inspect().state is ConfigState.UNKNOWN


def test_codex_auth_commit_failure_restores_config(tmp_path: Path, monkeypatch) -> None:
    config_dir = tmp_path / ".codex"
    config_dir.mkdir()
    original = (
        'model_provider = "proxy"\n\n'
        "[model_providers.proxy]\n"
        'base_url = "https://old.example"\n'
        'wire_api = "responses"\n'
        "requires_openai_auth = true\n"
    )
    (config_dir / "config.toml").write_text(original)
    (config_dir / "auth.json").write_text(
        json.dumps({"OPENAI_API_KEY": "old-secret", "auth_mode": "apikey"})
    )
    adapter = CodexAdapter(tmp_path, _registry())
    change = adapter.prepare_provider(
        {
            "provider_id": "proxy",
            "base_url": "https://new.example",
            "api_key": "new-secret",
            "wire_api": "responses",
            "requires_openai_auth": True,
        }
    )
    real_write = codex_module.atomic_write_private

    def fail_auth(path: Path, content: bytes, root: Path) -> None:
        if path.name == "auth.json":
            raise OSError("injected auth failure")
        real_write(path, content, root)

    monkeypatch.setattr(codex_module, "atomic_write_private", fail_auth)
    with pytest.raises(OSError, match="injected"):
        adapter.commit(change)
    assert (config_dir / "config.toml").read_text() == original


def test_codex_auth_and_internal_config_rollback_failure_requires_recovery(
    tmp_path: Path, monkeypatch
) -> None:
    config_dir = tmp_path / ".codex"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text(
        'model_provider = "proxy"\n\n'
        "[model_providers.proxy]\n"
        'base_url = "https://old.example"\n'
        'wire_api = "responses"\n'
        "requires_openai_auth = true\n"
    )
    (config_dir / "auth.json").write_text(
        json.dumps({"OPENAI_API_KEY": "old-secret", "auth_mode": "apikey"})
    )
    adapter = CodexAdapter(tmp_path, _registry())
    change = adapter.prepare_provider(
        {
            "provider_id": "proxy",
            "base_url": "https://new.example",
            "api_key": "new-secret",
            "wire_api": "responses",
            "requires_openai_auth": True,
        }
    )
    real_write = codex_module.atomic_write_private
    config_writes = 0

    def fail_auth_and_rollback(path: Path, content: bytes, root: Path) -> None:
        nonlocal config_writes
        if path.name == "auth.json":
            raise OSError("injected auth failure")
        config_writes += 1
        if config_writes > 1:
            raise OSError("injected config rollback failure")
        real_write(path, content, root)

    monkeypatch.setattr(codex_module, "atomic_write_private", fail_auth_and_rollback)
    with pytest.raises(RecoveryRequiredError, match="rollback both failed"):
        adapter.commit(change)


def test_codex_session_commit_failure_restores_auth_and_config(tmp_path: Path, monkeypatch) -> None:
    config_dir = tmp_path / ".codex"
    config_dir.mkdir()
    original_config = (
        'model_provider = "proxy"\n\n'
        "[model_providers.proxy]\n"
        'base_url = "https://old.example"\n'
        'wire_api = "responses"\n'
        "requires_openai_auth = true\n"
    )
    original_auth = json.dumps({"OPENAI_API_KEY": "old-secret", "auth_mode": "apikey"})
    (config_dir / "config.toml").write_text(original_config)
    (config_dir / "auth.json").write_text(original_auth)
    sessions = config_dir / "sessions" / "2026" / "08" / "08"
    sessions.mkdir(parents=True)
    (sessions / "session.jsonl").write_text(
        json.dumps({"type": "session_meta", "payload": {"id": "sid", "model_provider": "openai"}})
        + "\n"
    )
    adapter = CodexAdapter(tmp_path, _registry())
    change = adapter.prepare_provider(
        {
            "provider_id": "proxy",
            "base_url": "https://new.example",
            "api_key": "new-secret",
            "wire_api": "responses",
            "requires_openai_auth": True,
        }
    )
    assert change.session is not None

    import codelux.adapters.codex as module

    real_session_commit = module.CodexSessionManager.commit
    monkeypatch.setattr(
        module.CodexSessionManager,
        "commit",
        lambda self, session: (_ for _ in ()).throw(OSError("injected session failure")),
    )
    with pytest.raises(OSError, match="injected session failure"):
        adapter.commit(change)
    assert (config_dir / "config.toml").read_text() == original_config
    assert (config_dir / "auth.json").read_text() == original_auth
    monkeypatch.setattr(module.CodexSessionManager, "commit", real_session_commit)


@pytest.mark.parametrize(
    ("auth", "expected"),
    [
        ({"auth_mode": "chatgpt", "tokens": {"access_token": "token"}}, ConfigState.OFFICIAL_LOGIN),
        ({"auth_mode": "apikey", "OPENAI_API_KEY": "key"}, ConfigState.OFFICIAL_API_KEY),
    ],
)
def test_codex_official_states(
    tmp_path: Path, monkeypatch, auth: dict[str, object], expected: ConfigState
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    config_dir = tmp_path / ".codex"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text('model_provider = "openai"\n')
    (config_dir / "auth.json").write_text(json.dumps(auth))
    assert CodexAdapter(tmp_path).inspect().state is expected


def test_codex_official_login_defaults_to_openai_without_selector(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    config_dir = tmp_path / ".codex"
    config_dir.mkdir()
    original = (
        '[projects."/work"]\n'
        'trust_level = "trusted"\n\n'
        "[tui.model_availability_nux]\n"
        '"gpt-5.6-sol" = true\n'
    )
    (config_dir / "config.toml").write_text(original)
    (config_dir / "auth.json").write_text(
        json.dumps({"auth_mode": "chatgpt", "tokens": {"access_token": "token"}})
    )

    observed = CodexAdapter(tmp_path).inspect()

    assert observed.state is ConfigState.OFFICIAL_LOGIN
    assert observed.provider_id == "openai"
    assert (config_dir / "config.toml").read_text() == original


def test_prepare_native_official_restore_repairs_custom_selector(tmp_path: Path) -> None:
    config_dir = tmp_path / ".codex"
    config_dir.mkdir()
    original = (
        'model_provider = "custom"\n\n'
        "[model_providers.custom]\n"
        'base_url = "https://proxy.example"\n'
    )
    (config_dir / "config.toml").write_text(original)
    auth = json.dumps({"auth_mode": "chatgpt", "tokens": {"access_token": "token"}})
    (config_dir / "auth.json").write_text(auth)

    adapter = CodexAdapter(tmp_path)
    assert adapter.has_native_official_login()
    change = adapter.prepare_native_official_restore()
    after_config = next(
        file.content for file in change.after if file.path == config_dir / "config.toml"
    ).decode()
    assert 'model_provider = "custom"' in after_config
    assert "[model_providers.custom]" in after_config
    custom_section = after_config.split("[model_providers.custom]", 1)[1]
    assert 'name = "OpenAI"' in custom_section
    assert "base_url" not in custom_section
    assert (
        next(
            file.content for file in change.after if file.path == config_dir / "auth.json"
        ).decode()
        == auth
    )

    isolated = adapter.prepare_native_official_restore(shared_session=False)
    isolated_config = next(
        file.content for file in isolated.after if file.path == config_dir / "config.toml"
    ).decode()
    assert 'model_provider = "openai"' in isolated_config


def test_native_openai_config_normalizes_without_losing_unknown_fields(tmp_path: Path) -> None:
    config_dir = tmp_path / ".codex"
    config_dir.mkdir()
    original = (
        'model_provider = "openai"\n'
        'approval_policy = "on-request"\n\n'
        '[projects."/work"]\ntrust_level = "trusted"\n'
    )
    (config_dir / "config.toml").write_text(original)
    (config_dir / "auth.json").write_text(
        json.dumps({"auth_mode": "chatgpt", "tokens": {"access_token": "token"}})
    )

    change = CodexAdapter(tmp_path).prepare_native_official_restore()
    normalized = next(
        file.content for file in change.after if file.path == config_dir / "config.toml"
    ).decode()

    assert 'model_provider = "custom"' in normalized
    assert 'approval_policy = "on-request"' in normalized
    assert '[projects."/work"]\ntrust_level = "trusted"' in normalized
    custom_section = normalized.split("[model_providers.custom]", 1)[1]
    assert 'name = "OpenAI"' in custom_section
    assert "base_url" not in custom_section


def test_codex_provider_patch_adds_selector_to_default_config(tmp_path: Path) -> None:
    config_dir = tmp_path / ".codex"
    config_dir.mkdir()
    original = '[projects."/work"]\ntrust_level = "trusted"\n'
    (config_dir / "config.toml").write_text(original)
    (config_dir / "auth.json").write_text(
        json.dumps({"auth_mode": "chatgpt", "tokens": {"access_token": "token"}})
    )

    change = CodexAdapter(tmp_path).prepare_provider(
        {
            "provider_id": "proxy",
            "base_url": "https://new.example",
            "api_key": "new-secret",
            "wire_api": "responses",
            "requires_openai_auth": True,
        }
    )
    patched = next(
        file.content for file in change.after if file.path == config_dir / "config.toml"
    ).decode()

    assert patched.startswith('model_provider = "proxy"\n' + original)
    assert "[model_providers.proxy]" in patched
    assert 'name = "proxy"' in patched


def test_codex_provider_patch_repairs_missing_native_provider_name(tmp_path: Path) -> None:
    config_dir = tmp_path / ".codex"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text(
        'model_provider = "custom"\n'
        "[model_providers.custom]\n"
        'base_url = "https://new.example"\n'
        'wire_api = "responses"\n'
        "requires_openai_auth = true\n"
        "custom_timeout = 60\n"
    )
    (config_dir / "auth.json").write_text(
        json.dumps({"auth_mode": "apikey", "OPENAI_API_KEY": "old-secret"})
    )

    change = CodexAdapter(tmp_path, _registry()).prepare_provider(
        {
            "provider_id": "proxy",
            "base_url": "https://new.example",
            "api_key": "new-secret",
            "wire_api": "responses",
            "requires_openai_auth": True,
        }
    )
    patched = next(
        file.content for file in change.after if file.path == config_dir / "config.toml"
    ).decode()

    assert '[model_providers.custom]\nname = "proxy"\n' in patched
    assert "custom_timeout = 60" in patched


def test_codex_provider_patch_converts_official_shared_alias(tmp_path: Path) -> None:
    config_dir = tmp_path / ".codex"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text(
        'model_provider = "custom"\n\n'
        "[model_providers.custom]\n"
        'name = "OpenAI"\n'
        'wire_api = "responses"\n'
        "requires_openai_auth = true\n"
    )
    (config_dir / "auth.json").write_text(
        json.dumps({"auth_mode": "chatgpt", "tokens": {"access_token": "token"}})
    )

    change = CodexAdapter(tmp_path, _registry()).prepare_provider(
        {
            "provider_id": "proxy",
            "base_url": "https://new.example",
            "api_key": "new-secret",
            "wire_api": "responses",
            "requires_openai_auth": True,
        }
    )
    patched = next(
        file.content for file in change.after if file.path == config_dir / "config.toml"
    ).decode()

    assert 'model_provider = "custom"' in patched
    custom_section = patched.split("[model_providers.custom]", 1)[1]
    assert 'name = "proxy"' in custom_section
    assert 'base_url = "https://new.example"' in custom_section
    assert 'wire_api = "responses"' in custom_section
    assert "requires_openai_auth = true" in custom_section


def test_codex_provider_patch_rejects_nonofficial_table_missing_base_url(
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / ".codex"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text(
        'model_provider = "custom"\n\n'
        "[model_providers.custom]\n"
        'name = "not-official"\n'
        'wire_api = "responses"\n'
        "requires_openai_auth = true\n"
    )
    (config_dir / "auth.json").write_text(
        json.dumps({"auth_mode": "apikey", "OPENAI_API_KEY": "old-secret"})
    )

    with pytest.raises(ValidationError, match="owned Provider fields are missing"):
        CodexAdapter(tmp_path, _registry()).prepare_provider(
            {
                "provider_id": "proxy",
                "base_url": "https://new.example",
                "api_key": "new-secret",
                "wire_api": "responses",
                "requires_openai_auth": True,
            }
        )


def test_codex_custom_and_conflicting_environment_states(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    config_dir = tmp_path / ".codex"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text(
        'model_provider = "proxy"\n\n'
        "[model_providers.proxy]\n"
        'base_url = "https://new.example"\n'
        'wire_api = "responses"\n'
        "requires_openai_auth = true\n"
    )
    (config_dir / "auth.json").write_text(
        json.dumps({"auth_mode": "apikey", "OPENAI_API_KEY": "new-secret"})
    )
    adapter = CodexAdapter(tmp_path, _registry())
    assert adapter.inspect().state is ConfigState.CUSTOM
    monkeypatch.setenv("OPENAI_API_KEY", "different-secret")
    assert adapter.inspect().state is ConfigState.EXTERNAL_OVERRIDE


def test_codex_custom_rejects_registered_api_key_drift(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    config_dir = tmp_path / ".codex"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text(
        'model_provider = "proxy"\n[model_providers.proxy]\n'
        'base_url = "https://new.example"\nwire_api = "responses"\n'
        "requires_openai_auth = true\n"
    )
    (config_dir / "auth.json").write_text(
        json.dumps({"auth_mode": "apikey", "OPENAI_API_KEY": "different"})
    )
    assert CodexAdapter(tmp_path, _registry()).inspect().state is ConfigState.UNKNOWN


def test_codex_unregistered_provider_exposes_detected_url_for_adoption(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    config_dir = tmp_path / ".codex"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text(
        'model_provider = "custom"\n[model_providers.custom]\n'
        'base_url = "https://new.example"\nwire_api = "responses"\n'
        "requires_openai_auth = true\n"
    )
    (config_dir / "auth.json").write_text(
        json.dumps({"auth_mode": "apikey", "OPENAI_API_KEY": "new-secret"})
    )
    observed = CodexAdapter(tmp_path, Registry()).inspect()
    assert observed.state is ConfigState.UNKNOWN
    assert observed.provider_id == "custom"
    assert observed.base_url == "https://new.example"


def test_codex_rejects_duplicate_owned_provider_key(tmp_path: Path) -> None:
    config_dir = tmp_path / ".codex"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text(
        'model_provider = "proxy"\n[model_providers.proxy]\n'
        'base_url = "https://old.example"\nbase_url = "https://new.example"\n'
        'wire_api = "responses"\nrequires_openai_auth = true\n'
    )
    (config_dir / "auth.json").write_text(
        json.dumps({"auth_mode": "apikey", "OPENAI_API_KEY": "new-secret"})
    )
    with pytest.raises(ValidationError, match="duplicate owned"):
        codex_module._parse_config((config_dir / "config.toml").read_bytes())


@pytest.mark.parametrize(
    ("config", "auth"),
    [
        ('model_provider = "missing"\n', {"auth_mode": "apikey", "OPENAI_API_KEY": "key"}),
        ('model_provider = "openai"\n', {"auth_mode": "unknown"}),
        (
            'model_provider = "proxy"\n[model_providers.proxy]\nbase_url = "https://new.example"\nwire_api = "responses"\nrequires_openai_auth = true\n',
            {"auth_mode": "chatgpt", "tokens": {"access_token": "token"}},
        ),
    ],
)
def test_codex_inconsistent_combinations_are_unknown(
    tmp_path: Path, monkeypatch, config: str, auth: dict[str, object]
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    config_dir = tmp_path / ".codex"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text(config)
    (config_dir / "auth.json").write_text(json.dumps(auth))
    assert CodexAdapter(tmp_path, _registry()).inspect().state is ConfigState.UNKNOWN


def test_codex_inspect_ignores_modern_unknown_tables(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    config_dir = tmp_path / ".codex"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text(
        'model_provider = "proxy"\n\n'
        '[tui]\nstatus_line = ["model", "current-dir"]\n\n'
        '[projects."/work"]\ntrust_level = "trusted"\n\n'
        '[model_providers.proxy]\nbase_url = "https://new.example"\n'
        'wire_api = "responses"\nrequires_openai_auth = true\n'
    )
    (config_dir / "auth.json").write_text(
        json.dumps({"auth_mode": "apikey", "OPENAI_API_KEY": "new-secret"})
    )
    assert CodexAdapter(tmp_path, _registry()).inspect().state is ConfigState.CUSTOM
