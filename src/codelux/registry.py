"""Provider registry schema and validation."""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from codelux.errors import ValidationError

PROVIDER_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
RESERVED_NAMES = {
    "add",
    "config",
    "edit",
    "list",
    "official",
    "reconcile",
    "recover",
    "remove",
    "rename",
    "status",
    "switch",
    "update",
    "version",
}
SUPPORTED_CLIENTS = {"claude", "codex"}


@dataclass(frozen=True)
class ClientBinding:
    base_url: str
    api_key: str = field(repr=False)
    wire_api: Optional[str] = None
    requires_openai_auth: Optional[bool] = None
    enabled: bool = True

    def __post_init__(self) -> None:
        parsed = urlparse(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValidationError("base_url must be an absolute HTTP(S) URL")
        if not self.api_key:
            raise ValidationError("api_key must not be empty")
        if self.wire_api not in {None, "responses", "chat_completions"}:
            raise ValidationError("unsupported wire_api")

    def to_dict(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "enabled": self.enabled,
            "base_url": self.base_url,
            "api_key": self.api_key,
        }
        if self.wire_api is not None:
            result["wire_api"] = self.wire_api
        if self.requires_openai_auth is not None:
            result["requires_openai_auth"] = self.requires_openai_auth
        return result

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ClientBinding":
        try:
            enabled = _optional_bool(raw, "enabled", default=True)
            if enabled is None:
                raise ValidationError("enabled must be a boolean")
            return cls(
                base_url=_required_string(raw, "base_url"),
                api_key=_required_string(raw, "api_key"),
                wire_api=raw.get("wire_api"),
                requires_openai_auth=_optional_bool(raw, "requires_openai_auth"),
                enabled=enabled,
            )
        except KeyError as exc:
            raise ValidationError(f"client binding missing field: {exc.args[0]}") from exc


@dataclass(frozen=True)
class ProviderRecord:
    name: str
    clients: Mapping[str, ClientBinding]
    description: str = ""

    def __post_init__(self) -> None:
        validate_provider_name(self.name)
        if not self.clients:
            raise ValidationError("provider must contain at least one client binding")
        unknown = set(self.clients).difference(SUPPORTED_CLIENTS)
        if unknown:
            raise ValidationError("unsupported clients: " + ", ".join(sorted(unknown)))


@dataclass(frozen=True)
class Registry:
    schema_version: int = 1
    providers: Mapping[str, ProviderRecord] = field(default_factory=dict)
    current: Mapping[str, Optional[str]] = field(default_factory=dict)

    @property
    def desired(self) -> Mapping[str, Optional[str]]:
        """Return the last successfully committed logical Provider per client."""
        return self.current

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValidationError("unsupported registry schema_version")
        for key, provider in self.providers.items():
            if key != provider.name:
                raise ValidationError("provider registry key must match provider name")
        unknown_current = set(self.current).difference(SUPPORTED_CLIENTS)
        if unknown_current:
            raise ValidationError("current contains unsupported clients")
        for client, provider_name in self.current.items():
            if provider_name is None:
                continue
            current_provider = self.providers.get(provider_name)
            if current_provider is None or client not in current_provider.clients:
                raise ValidationError("current must reference a registered client binding")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "providers": {
                name: {
                    "name": provider.name,
                    "description": provider.description,
                    "clients": {
                        client: binding.to_dict() for client, binding in provider.clients.items()
                    },
                }
                for name, provider in self.providers.items()
            },
            "current": dict(self.current),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Registry":
        try:
            providers = {
                name: ProviderRecord(
                    name=str(item["name"]),
                    description=str(item.get("description", "")),
                    clients={
                        client: ClientBinding.from_dict(binding)
                        for client, binding in item["clients"].items()
                    },
                )
                for name, item in raw["providers"].items()
            }
            return cls(
                schema_version=int(raw["schema_version"]),
                providers=providers,
                current=dict(raw.get("current", {})),
            )
        except KeyError as exc:
            raise ValidationError(f"registry missing field: {exc.args[0]}") from exc


def validate_provider_name(name: str) -> None:
    if not PROVIDER_NAME.fullmatch(name):
        raise ValidationError("invalid provider name")
    if name in RESERVED_NAMES:
        raise ValidationError("provider name is reserved")


def _required_string(raw: Mapping[str, Any], key: str) -> str:
    value = raw[key]
    if not isinstance(value, str):
        raise ValidationError(f"{key} must be a string")
    return value


def _optional_bool(
    raw: Mapping[str, Any], key: str, default: Optional[bool] = None
) -> Optional[bool]:
    value = raw.get(key, default)
    if value is not None and not isinstance(value, bool):
        raise ValidationError(f"{key} must be a boolean")
    return value
