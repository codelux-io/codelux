import pytest


@pytest.fixture(autouse=True)
def isolate_client_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep host client credentials and routing out of every test by default."""
    for name in (
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_AUTH_TOKEN",
        "OPENAI_API_KEY",
        "CODELUX_CLAUDE_API_KEY",
        "CODELUX_CODEX_API_KEY",
        "CODELUX_TEST_HOME",
    ):
        monkeypatch.delenv(name, raising=False)
