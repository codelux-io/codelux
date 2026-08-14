from pathlib import Path

import pytest

from codelux.errors import ValidationError
from codelux.registry import ClientBinding, ProviderRecord, Registry
from codelux.registry_io import load_registry


def test_registry_round_trip() -> None:
    binding = ClientBinding("https://api.example.com", "test-secret")
    registry = Registry(
        providers={"proxy": ProviderRecord("proxy", {"claude": binding})},
        current={"claude": "proxy"},
    )

    assert Registry.from_dict(registry.to_dict()) == registry
    assert registry.desired == registry.current
    assert "test-secret" not in repr(binding)


@pytest.mark.parametrize("name", ["official", "Switch", "bad.name", ""])
def test_registry_rejects_invalid_or_reserved_names(name: str) -> None:
    with pytest.raises(ValidationError):
        ProviderRecord(name, {"claude": ClientBinding("https://example.com", "secret")})


def test_binding_rejects_string_boolean() -> None:
    with pytest.raises(ValidationError, match="boolean"):
        ClientBinding.from_dict(
            {"base_url": "https://example.com", "api_key": "secret", "enabled": "false"}
        )


@pytest.mark.parametrize("payload", ["[]", '{"schema_version":1,"providers":[]}'])
def test_malformed_registry_shape_is_a_validation_error(tmp_path: Path, payload: str) -> None:
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / "providers.json").write_text(payload)
    with pytest.raises(ValidationError, match="registry is invalid"):
        load_registry(tmp_path)
