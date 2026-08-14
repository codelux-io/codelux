"""Codex adapter with a conservative, text-preserving TOML patcher."""

import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

from codelux.adapters.base import ClientAdapter, config_file
from codelux.errors import ValidationError
from codelux.models import ConfigFile, ConfigState, ObservedConfig, PreparedChange, ProcessState
from codelux.registry import Registry
from codelux.safe_files import atomic_write_private
from codelux.sessions import CodexSessionManager


ROOT_ASSIGNMENT = re.compile(
    r"^(?P<indent>\s*)model_provider\s*=\s*(?P<value>\"[^\"]*\"|'[^']*')\s*$"
)
TABLE = re.compile(r"^\[model_providers\.(?P<name>[a-z0-9][a-z0-9_-]{0,31})\]$")
KEY = re.compile(
    r"^(?P<indent>\s*)(?P<key>base_url|wire_api|requires_openai_auth)\s*=\s*" r"(?P<value>.+?)\s*$"
)
PROVIDER_NAME = re.compile(r"^(?P<indent>\s*)name\s*=\s*(?P<value>\"[^\"]*\"|'[^']*')\s*$")


class CodexAdapter(ClientAdapter):
    name = "codex"

    def __init__(self, home: Optional[Path] = None, registry: Optional[Registry] = None) -> None:
        self.home = (home or Path.home()).absolute()
        self.config_path = self.home / ".codex" / "config.toml"
        self.auth_path = self.home / ".codex" / "auth.json"
        self.config_root = self.config_path.parent
        self.registry = registry

    def is_installed(self) -> bool:
        return self.config_path.exists() or shutil.which("codex") is not None

    def is_running(self) -> ProcessState:
        try:
            result = subprocess.run(
                ["ps", "-axo", "pid=,command="],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except (OSError, subprocess.SubprocessError):
            return ProcessState.UNKNOWN
        for raw_line in result.stdout.splitlines():
            command = raw_line.strip()
            if not command or command.startswith(b"ps "):
                continue
            tokens = command.split()
            # `ps pid=,command=` prefixes each row with the numeric PID.
            command_tokens = tokens[1:] if tokens and tokens[0].isdigit() else tokens
            if not command_tokens:
                continue
            first = command_tokens[0].rsplit(b"/", 1)[-1]
            # Arguments may contain "codex" (for example, codelux --client
            # codex), so only the executable token identifies the client.
            # The npm CLI is commonly launched as `node .../bin/codex`, and
            # the native vendor binary may be a later command token.
            node_launcher = first in {b"node", b"nodejs"} and any(
                token.rsplit(b"/", 1)[-1] in {b"codex", b"codex-cli"}
                for token in command_tokens[1:]
            )
            if first in {b"codex", b"codex-cli"} or node_launcher:
                return ProcessState.RUNNING
        return ProcessState.NOT_RUNNING

    def inspect(self) -> ObservedConfig:
        config, _, config_reasons = self._read_config()
        auth, _, auth_reasons = self._read_auth()
        reasons = config_reasons + auth_reasons
        if config is None or auth is None:
            return ObservedConfig(ConfigState.UNKNOWN, None, None, None, tuple(reasons))
        provider_id = config["model_provider"]
        provider = config["providers"].get(provider_id)
        if provider_id != "openai" and provider is None:
            return ObservedConfig(
                ConfigState.UNKNOWN, provider_id, None, None, ("provider table missing",)
            )
        external_key = os.environ.get("OPENAI_API_KEY")
        auth_mode = auth.get("auth_mode")
        api_key = auth.get("OPENAI_API_KEY")
        if external_key is not None and (auth_mode != "apikey" or external_key != api_key):
            return ObservedConfig(
                ConfigState.EXTERNAL_OVERRIDE,
                provider_id,
                None,
                None,
                ("OPENAI_API_KEY conflicts",),
            )
        if auth_mode == "chatgpt":
            tokens = auth.get("tokens")
            official_provider = provider_id == "openai" or (
                provider_id == "custom"
                and isinstance(provider, dict)
                and provider.get("base_url") is None
                and provider.get("wire_api") == "responses"
                and provider.get("requires_openai_auth") is True
            )
            if official_provider and isinstance(tokens, dict) and any(tokens.values()):
                return ObservedConfig(
                    ConfigState.OFFICIAL_LOGIN,
                    "openai",
                    None,
                    _fingerprint(auth),
                    tuple(reasons),
                )
            return ObservedConfig(
                ConfigState.UNKNOWN, provider_id, None, None, ("chatgpt tokens invalid",)
            )
        if auth_mode != "apikey" or not isinstance(api_key, str) or not api_key:
            return ObservedConfig(
                ConfigState.UNKNOWN, provider_id, None, None, ("auth mode invalid",)
            )
        if provider_id == "openai":
            return ObservedConfig(
                ConfigState.OFFICIAL_API_KEY,
                "openai",
                None,
                _fingerprint(auth),
                tuple(reasons),
            )
        logical_provider_id = provider_id
        if provider_id == "custom" and self.registry is not None:
            current = self.registry.current.get("codex")
            if isinstance(current, str) and current in self.registry.providers:
                logical_provider_id = current
            else:
                candidates = [
                    name
                    for name, record in self.registry.providers.items()
                    if (candidate := record.clients.get("codex")) is not None
                    and isinstance(provider, dict)
                    and candidate.base_url == provider.get("base_url")
                    and candidate.api_key == api_key
                ]
                if len(candidates) == 1:
                    logical_provider_id = candidates[0]
        if self.registry is None or logical_provider_id not in self.registry.providers:
            return ObservedConfig(
                ConfigState.UNKNOWN,
                logical_provider_id,
                provider.get("base_url") if isinstance(provider, dict) else None,
                None,
                ("provider not registered",),
            )
        binding = self.registry.providers[logical_provider_id].clients.get("codex")
        if (
            binding is None
            or binding.api_key != api_key
            or not _binding_matches(provider, binding.to_dict())
        ):
            return ObservedConfig(
                ConfigState.UNKNOWN,
                logical_provider_id,
                provider.get("base_url"),
                None,
                ("provider binding mismatch",),
            )
        return ObservedConfig(
            ConfigState.CUSTOM,
            logical_provider_id,
            provider.get("base_url"),
            _fingerprint({"provider": provider, "api_key": api_key}),
            tuple(reasons),
        )

    def prepare_provider(
        self,
        binding: Mapping[str, object],
        *,
        shared_session: bool = True,
        session_sources: Optional[set[str]] = None,
        migrate_sessions: bool = True,
    ) -> PreparedChange:
        config, config_raw, _ = self._read_config()
        auth, auth_raw, _ = self._read_auth()
        if config is None or auth is None:
            raise ValidationError("Codex configuration is not readable")
        logical_provider_id = binding.get("provider_id", binding.get("name", "custom"))
        if not isinstance(logical_provider_id, str):
            raise ValidationError("Codex binding requires a provider_id")
        provider_id = (
            "custom" if shared_session and self.registry is not None else logical_provider_id
        )
        after_config = _patch_config(config_raw, provider_id, binding)
        after_auth = (
            json.dumps(
                {"OPENAI_API_KEY": binding.get("api_key"), "auth_mode": "apikey"},
                indent=2,
            ).encode()
            + b"\n"
        )
        session = None
        if shared_session and migrate_sessions:
            sources = set(session_sources or {"openai"})
            current_provider = config.get("model_provider") if config else None
            if isinstance(current_provider, str):
                sources.add(current_provider)
            session = CodexSessionManager(self.home).prepare(sources)
        detected = self.inspect()
        if detected.state is not ConfigState.EXTERNAL_OVERRIDE and _has_chatgpt_login(auth_raw):
            detected = ObservedConfig(
                ConfigState.OFFICIAL_LOGIN,
                "openai",
                None,
                _fingerprint(auth),
                detected.reasons,
            )
        change = PreparedChange(
            "codex",
            (config_file(self.config_path, config_raw), config_file(self.auth_path, auth_raw)),
            (config_file(self.config_path, after_config), config_file(self.auth_path, after_auth)),
            detected,
            session,
        )
        self.validate_files(change.after)
        return change

    def prepare_snapshot_restore(
        self,
        manifest: Mapping[str, object],
        *,
        shared_session: bool = True,
        session_sources: Optional[set[str]] = None,
    ) -> PreparedChange:
        config_before = self.config_path.read_bytes()
        auth_before = self.auth_path.read_bytes()
        config_after = _snapshot_file(manifest, "codex/config.toml", self.home / ".codelux")
        auth_after = _snapshot_file(manifest, "codex/auth.json", self.home / ".codelux")
        if _has_chatgpt_login(auth_after):
            config_after = _configure_official_config(config_after, shared_session)
        session = (
            CodexSessionManager(self.home).prepare(set(session_sources or {"openai"}))
            if shared_session
            else None
        )
        change = PreparedChange(
            "codex",
            (
                config_file(self.config_path, config_before),
                config_file(self.auth_path, auth_before),
            ),
            (config_file(self.config_path, config_after), config_file(self.auth_path, auth_after)),
            self.inspect(),
            session,
        )
        self.validate_files(change.after)
        return change

    def has_native_official_login(self) -> bool:
        """Return whether Codex currently has a usable ChatGPT login."""
        _, raw, _ = self._read_auth()
        return _has_chatgpt_login(raw)

    def prepare_native_official_restore(
        self,
        *,
        shared_session: bool = True,
        session_sources: Optional[set[str]] = None,
    ) -> PreparedChange:
        """Select the built-in OpenAI Provider while preserving native login state."""
        config, config_raw, _ = self._read_config()
        auth, auth_raw, _ = self._read_auth()
        if config is None or auth is None or not _has_chatgpt_login(auth_raw):
            raise ValidationError("Codex official login is not available")
        session = (
            CodexSessionManager(self.home).prepare(set(session_sources or {"openai"}))
            if shared_session
            else None
        )
        change = PreparedChange(
            "codex",
            (config_file(self.config_path, config_raw), config_file(self.auth_path, auth_raw)),
            (
                config_file(
                    self.config_path,
                    _configure_official_config(config_raw, shared_session),
                ),
                config_file(self.auth_path, auth_raw),
            ),
            self.inspect(),
            session,
        )
        self.validate_files(change.after)
        return change

    def validate_files(self, files: Tuple[ConfigFile, ...]) -> None:
        if len(files) != 2 or {f.path for f in files} != {self.config_path, self.auth_path}:
            raise ValidationError("Codex change must contain config.toml and auth.json")
        _parse_config(next(f.content for f in files if f.path == self.config_path))
        try:
            auth = json.loads(next(f.content for f in files if f.path == self.auth_path))
        except json.JSONDecodeError as exc:
            raise ValidationError("candidate Codex auth.json is invalid JSON") from exc
        if not isinstance(auth, dict):
            raise ValidationError("candidate Codex auth.json must be an object")
        if auth.get("auth_mode") == "apikey":
            if not auth.get("OPENAI_API_KEY"):
                raise ValidationError("candidate Codex auth.json must contain an API key")
        elif auth.get("auth_mode") == "chatgpt":
            if not isinstance(auth.get("tokens"), dict) or not any(auth["tokens"].values()):
                raise ValidationError("candidate Codex ChatGPT auth is incomplete")
        else:
            raise ValidationError("candidate Codex auth mode is unsupported")

    def commit(self, change: PreparedChange) -> None:
        self.validate_files(change.after)
        config = next(f for f in change.after if f.path == self.config_path)
        auth = next(f for f in change.after if f.path == self.auth_path)
        before_config = next(f for f in change.before if f.path == self.config_path)
        before_auth = next(f for f in change.before if f.path == self.auth_path)
        atomic_write_private(self.config_path, config.content, self.config_root)
        auth_written = False
        try:
            atomic_write_private(self.auth_path, auth.content, self.config_root)
            auth_written = True
            if change.session is not None:
                CodexSessionManager(self.home).commit(change.session)
        except Exception as exc:
            try:
                if change.session is not None:
                    CodexSessionManager(self.home).rollback(change.session)
                if auth_written:
                    atomic_write_private(self.auth_path, before_auth.content, self.config_root)
                atomic_write_private(self.config_path, before_config.content, self.config_root)
            except Exception as rollback_exc:
                from codelux.errors import RecoveryRequiredError

                raise RecoveryRequiredError(
                    "Codex auth write and config rollback both failed"
                ) from rollback_exc
            raise exc

    def rollback(self, change: PreparedChange) -> None:
        if change.session is not None:
            CodexSessionManager(self.home).rollback(change.session)
        for file in sorted(change.before, key=lambda item: item.path.name, reverse=True):
            atomic_write_private(file.path, file.content, self.config_root)

    def _read_config(self) -> Tuple[Optional[dict], bytes, list]:
        try:
            raw = self.config_path.read_bytes()
            return _parse_config(raw), raw, []
        except (OSError, ValidationError) as exc:
            return None, b"", [f"config unreadable: {type(exc).__name__}"]

    def _read_auth(self) -> Tuple[Optional[dict], bytes, list]:
        try:
            raw = self.auth_path.read_bytes()
            auth = json.loads(raw)
            if not isinstance(auth, dict):
                raise ValidationError("auth root must be an object")
            return auth, raw, []
        except (OSError, json.JSONDecodeError, ValidationError) as exc:
            return None, b"", [f"auth unreadable: {type(exc).__name__}"]


def _parse_config(raw: bytes) -> dict:
    text = raw.decode("utf-8")
    provider_id = None
    root_seen = False
    providers: Dict[str, Dict[str, Any]] = {}
    current: Optional[str] = None
    at_root = True
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        root = ROOT_ASSIGNMENT.match(stripped) if at_root else None
        if root:
            if root_seen:
                raise ValidationError("duplicate model_provider assignment")
            root_seen = True
            provider_id = root.group("value")[1:-1]
            continue
        table = TABLE.match(stripped)
        if table:
            at_root = False
            current = table.group("name")
            if current in providers:
                raise ValidationError("duplicate Provider table")
            providers[current] = {}
            continue
        if stripped.startswith("["):
            # Normal Codex tables are outside the Provider patch ownership.
            at_root = False
            current = None
            continue
        key = KEY.match(stripped)
        if key and current:
            if key.group("key") in providers[current]:
                raise ValidationError("duplicate owned Provider key")
            providers[current][key.group("key")] = _toml_value(key.group("value"))
    # Current Codex releases omit the selector for the built-in OpenAI
    # provider.  An absent selector is therefore the official default, while
    # an explicitly empty selector remains invalid.
    if provider_id is None:
        provider_id = "openai"
    elif not provider_id:
        raise ValidationError("model_provider is empty")
    return {"model_provider": provider_id, "providers": providers}


def _toml_value(value: str) -> object:
    if value.startswith(('"', "'")) and value[-1:] == value[0]:
        return value[1:-1]
    if value == "true":
        return True
    if value == "false":
        return False
    raise ValidationError("unsupported TOML value")


def _patch_config(raw: bytes, provider_id: str, binding: Mapping[str, object]) -> bytes:
    text = raw.decode("utf-8")
    lines = text.splitlines()
    display_name = binding.get("provider_id", provider_id)
    if not isinstance(display_name, str) or not display_name:
        raise ValidationError("invalid Codex Provider name")
    table_index = next(
        (i for i, line in enumerate(lines) if line.strip() == f"[model_providers.{provider_id}]"),
        None,
    )
    if table_index is None:
        if not provider_id or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", provider_id):
            raise ValidationError("invalid Codex Provider name")
        values = {
            "base_url": binding.get("base_url"),
            "wire_api": binding.get("wire_api", "responses"),
            "requires_openai_auth": binding.get("requires_openai_auth", True),
        }
        if (
            not isinstance(values["base_url"], str)
            or not isinstance(values["wire_api"], str)
            or not isinstance(values["requires_openai_auth"], bool)
        ):
            raise ValidationError("invalid Codex Provider fields")
        root_indexes = [
            index for index, line in enumerate(lines) if ROOT_ASSIGNMENT.match(line.strip())
        ]
        if len(root_indexes) > 1:
            raise ValidationError("duplicate model_provider assignment")
        if root_indexes:
            lines[root_indexes[0]] = f'model_provider = "{provider_id}"'
        else:
            # Root assignments must precede tables in TOML.  Prefixing the
            # selector preserves every existing byte after the new line.
            lines.insert(0, f'model_provider = "{provider_id}"')
        updated = "\n".join(lines)
        if text.endswith("\n"):
            updated += "\n"
        suffix = "\n" if updated.endswith("\n") else "\n\n"
        rendered = (
            f"{suffix}[model_providers.{provider_id}]\n"
            f"name = {json.dumps(display_name)}\n"
            f"base_url = {json.dumps(values['base_url'])}\n"
            f"wire_api = {json.dumps(values['wire_api'])}\n"
            f"requires_openai_auth = {str(values['requires_openai_auth']).lower()}\n"
        )
        candidate = (updated + rendered).encode("utf-8")
        # The root selector and appended table are owned; all other existing
        # lines retain their original content and order.
        return candidate
    end = next(
        (i for i in range(table_index + 1, len(lines)) if lines[i].strip().startswith("[")),
        len(lines),
    )
    values = {
        "base_url": binding.get("base_url"),
        "wire_api": binding.get("wire_api", "responses"),
        "requires_openai_auth": binding.get("requires_openai_auth", True),
    }
    if (
        not isinstance(values["base_url"], str)
        or not isinstance(values["wire_api"], str)
        or not isinstance(values["requires_openai_auth"], bool)
    ):
        raise ValidationError("invalid Codex Provider fields")
    existing_names = []
    existing_values: Dict[str, object] = {}
    for line in lines[table_index + 1 : end]:
        stripped = line.strip()
        name_match = PROVIDER_NAME.match(stripped)
        if name_match:
            existing_names.append(name_match.group("value")[1:-1])
            continue
        match = KEY.match(stripped)
        if match:
            key = match.group("key")
            if key in existing_values:
                raise ValidationError("duplicate owned Provider key")
            existing_values[key] = _toml_value(match.group("value"))
    official_shared_alias = (
        provider_id == "custom"
        and existing_names == ["OpenAI"]
        and existing_values == {"wire_api": "responses", "requires_openai_auth": True}
    )
    found = set()
    name_seen = False
    for i in range(table_index + 1, end):
        name_match = PROVIDER_NAME.match(lines[i].strip())
        if name_match:
            if name_seen:
                raise ValidationError("duplicate Provider name")
            name_seen = True
            lines[i] = f'{name_match.group("indent")}name = {json.dumps(display_name)}'
            continue
        match = KEY.match(lines[i].strip())
        if match and match.group("key") in values:
            value = values[match.group("key")]
            if not isinstance(value, (str, bool)):
                raise ValidationError(f"invalid {match.group('key')}")
            rendered = json.dumps(value) if isinstance(value, str) else str(value).lower()
            lines[i] = f"{match.group('indent')}{match.group('key')} = {rendered}"
            found.add(match.group("key"))
    missing = set(values) - found
    if missing == {"base_url"} and official_shared_alias:
        lines.insert(end, f"base_url = {json.dumps(values['base_url'])}")
        found.add("base_url")
    if found != set(values):
        raise ValidationError("owned Provider fields are missing; refusing insertion")
    if not name_seen:
        lines.insert(table_index + 1, f"name = {json.dumps(display_name)}")
    root_updated = False
    for i, line in enumerate(lines):
        if ROOT_ASSIGNMENT.match(line.strip()):
            lines[i] = f'model_provider = "{provider_id}"'
            root_updated = True
            break
    if not root_updated:
        lines.insert(0, f'model_provider = "{provider_id}"')
    candidate = ("\n".join(lines).rstrip() + "\n").encode("utf-8")
    if _unknown_summary(raw, provider_id) != _unknown_summary(candidate, provider_id):
        raise ValidationError("unknown TOML field summary changed; refusing write")
    return candidate


def _set_model_provider(raw: bytes, provider_id: str) -> bytes:
    text = raw.decode("utf-8")
    lines = text.splitlines()
    root_indexes = [
        index for index, line in enumerate(lines) if ROOT_ASSIGNMENT.match(line.strip())
    ]
    if len(root_indexes) > 1:
        raise ValidationError("duplicate model_provider assignment")
    if root_indexes:
        lines[root_indexes[0]] = f'model_provider = "{provider_id}"'
    else:
        lines.insert(0, f'model_provider = "{provider_id}"')
    candidate = ("\n".join(lines) + ("\n" if text.endswith("\n") else "")).encode("utf-8")
    _parse_config(candidate)
    return candidate


def _configure_official_config(raw: bytes, shared_session: bool) -> bytes:
    if not shared_session:
        return _set_model_provider(raw, "openai")

    text = _set_model_provider(raw, "custom").decode("utf-8")
    lines = []
    skipping = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == "[model_providers.custom]":
            skipping = True
            continue
        if skipping and stripped.startswith("["):
            if stripped.startswith("[model_providers.custom."):
                continue
            skipping = False
        if not skipping:
            lines.append(line)
    normalized = "\n".join(lines).rstrip()
    normalized += (
        '\n\n[model_providers.custom]\nname = "OpenAI"\n'
        'wire_api = "responses"\nrequires_openai_auth = true\n'
    )
    candidate = normalized.encode("utf-8")
    parsed = _parse_config(candidate)
    if parsed["model_provider"] != "custom" or parsed["providers"].get("custom", {}).get(
        "base_url"
    ):
        raise ValidationError("official shared Provider normalization failed")
    return candidate


def _has_chatgpt_login(raw: bytes) -> bool:
    try:
        auth = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return False
    return (
        isinstance(auth, dict)
        and auth.get("auth_mode") == "chatgpt"
        and isinstance(auth.get("tokens"), dict)
        and any(auth["tokens"].values())
    )


def _binding_matches(provider: Mapping[str, object], binding: Mapping[str, object]) -> bool:
    return all(
        provider.get(key) == binding.get(key)
        for key in ("base_url", "wire_api", "requires_openai_auth")
    )


def _fingerprint(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def _unknown_summary(raw: bytes, provider_id: str) -> str:
    lines = []
    current = None
    for line in raw.splitlines(keepends=True):
        text = line.decode("utf-8")
        stripped = text.strip()
        if stripped.startswith("["):
            current = stripped
        if current == f"[model_providers.{provider_id}]" and (
            KEY.match(stripped) or PROVIDER_NAME.match(stripped)
        ):
            continue
        if current is None and ROOT_ASSIGNMENT.match(stripped):
            continue
        lines.append(line)
    return hashlib.sha256(b"".join(lines)).hexdigest()


def _snapshot_file(manifest: Mapping[str, object], source_path: str, root: Path) -> bytes:
    files = manifest.get("files")
    if not isinstance(files, list):
        raise ValidationError("snapshot files must be a list")
    for item in files:
        if not isinstance(item, dict):
            raise ValidationError("snapshot file entry must be an object")
        if item.get("source_path") == source_path:
            backup = root / str(item["backup_path"])
            if backup.is_symlink() or not backup.is_file():
                raise ValidationError("snapshot backup is missing or unsafe")
            return backup.read_bytes()
    raise ValidationError(f"snapshot does not contain {source_path}")
