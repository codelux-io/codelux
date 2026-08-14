import json
import subprocess
from pathlib import Path

from codelux.adapters.claude import ClaudeAdapter
from codelux.models import ConfigState, ProcessState
from codelux.registry import ClientBinding, ProviderRecord, Registry


def test_claude_prepare_does_not_modify_live_file(tmp_path: Path) -> None:
    settings_dir = tmp_path / ".claude"
    settings_dir.mkdir()
    settings_path = settings_dir / "settings.json"
    settings_path.write_text(json.dumps({"plugins": {"enabled": True}, "env": {}}))
    adapter = ClaudeAdapter(tmp_path)

    change = adapter.prepare_provider(
        {"base_url": "https://custom.example", "api_key": "test-secret"}
    )

    assert adapter.inspect().state is ConfigState.OFFICIAL_LOGIN
    assert json.loads(settings_path.read_text())["env"] == {}
    adapter.commit(change)
    observed = adapter.inspect()
    assert observed.state is ConfigState.CUSTOM
    assert observed.base_url == "https://custom.example"
    assert json.loads(settings_path.read_text())["plugins"] == {"enabled": True}
    adapter.rollback(change)
    assert adapter.inspect().state is ConfigState.OFFICIAL_LOGIN


def test_claude_matching_environment_is_observable_not_override(
    tmp_path: Path, monkeypatch
) -> None:
    settings_dir = tmp_path / ".claude"
    settings_dir.mkdir()
    (settings_dir / "settings.json").write_text(
        json.dumps(
            {
                "env": {
                    "ANTHROPIC_BASE_URL": "https://custom.example",
                    "ANTHROPIC_AUTH_TOKEN": "test-secret",
                }
            }
        )
    )
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://custom.example")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "test-secret")

    observed = ClaudeAdapter(tmp_path).inspect()
    assert observed.state is ConfigState.CUSTOM
    assert "matching value" in " ".join(observed.reasons)


def test_process_check_ignores_client_name_in_arguments(monkeypatch, tmp_path: Path) -> None:
    adapter = ClaudeAdapter(tmp_path)

    class Result:
        stdout = b"123 codelux switch proxy --client claude\n"

    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: Result())
    assert adapter.is_running() is ProcessState.NOT_RUNNING


def test_claude_official_api_key_state(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    settings = tmp_path / ".claude"
    settings.mkdir()
    (settings / "settings.json").write_text(
        json.dumps({"env": {"ANTHROPIC_AUTH_TOKEN": "official-key"}})
    )
    assert ClaudeAdapter(tmp_path).inspect().state is ConfigState.OFFICIAL_API_KEY


def test_claude_conflicting_environment_is_external_override(tmp_path: Path, monkeypatch) -> None:
    settings = tmp_path / ".claude"
    settings.mkdir()
    (settings / "settings.json").write_text(
        json.dumps({"env": {"ANTHROPIC_BASE_URL": "https://file.example"}})
    )
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://environment.example")
    assert ClaudeAdapter(tmp_path).inspect().state is ConfigState.EXTERNAL_OVERRIDE


def test_claude_malformed_settings_are_unknown(tmp_path: Path) -> None:
    settings = tmp_path / ".claude"
    settings.mkdir()
    (settings / "settings.json").write_text('{"env": []}')
    assert ClaudeAdapter(tmp_path).inspect().state is ConfigState.UNKNOWN


def test_claude_custom_requires_token_and_exact_registered_credential(tmp_path: Path) -> None:
    settings = tmp_path / ".claude"
    settings.mkdir()
    path = settings / "settings.json"
    registry = Registry(
        providers={
            "proxy": ProviderRecord(
                "proxy", {"claude": ClientBinding("https://custom.example", "registered")}
            )
        }
    )

    path.write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": "https://custom.example"}}))
    assert ClaudeAdapter(tmp_path, registry).inspect().state is ConfigState.UNKNOWN

    path.write_text(
        json.dumps(
            {
                "env": {
                    "ANTHROPIC_BASE_URL": "https://custom.example",
                    "ANTHROPIC_AUTH_TOKEN": "different",
                }
            }
        )
    )
    observed = ClaudeAdapter(tmp_path, registry).inspect()
    assert observed.state is ConfigState.CUSTOM
    assert observed.provider_id is None

    path.write_text(
        json.dumps(
            {
                "env": {
                    "ANTHROPIC_BASE_URL": "https://custom.example",
                    "ANTHROPIC_AUTH_TOKEN": "registered",
                }
            }
        )
    )
    assert ClaudeAdapter(tmp_path, registry).inspect().provider_id == "proxy"
