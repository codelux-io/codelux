"""Claude Code settings adapter."""

import hashlib
import json
import os
import shutil
import subprocess
from copy import deepcopy
from pathlib import Path
from typing import Mapping, Optional, Tuple

from codelux.adapters.base import ClientAdapter, config_file
from codelux.errors import ValidationError
from codelux.models import ConfigFile, ConfigState, ObservedConfig, PreparedChange, ProcessState
from codelux.registry import Registry
from codelux.safe_files import atomic_write_private


OFFICIAL_BASE_URLS = {"https://api.anthropic.com"}


class ClaudeAdapter(ClientAdapter):
    name = "claude"

    def __init__(self, home: Optional[Path] = None, registry: Optional[Registry] = None) -> None:
        self.home = (home or Path.home()).absolute()
        self.settings_path = self.home / ".claude" / "settings.json"
        self.config_root = self.settings_path.parent
        self.registry = registry

    def is_installed(self) -> bool:
        return self.settings_path.exists() or shutil.which("claude") is not None

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
        matches = []
        for raw_line in result.stdout.splitlines():
            command = raw_line.strip()
            if not command or command.startswith(b"ps "):
                continue
            tokens = command.split()
            command_tokens = tokens[1:] if tokens and tokens[0].isdigit() else tokens
            if not command_tokens:
                continue
            first = command_tokens[0].rsplit(b"/", 1)[-1]
            # Arguments may contain "claude" (for example, codelux --client
            # claude), so only the executable token identifies the client.
            node_launcher = first in {b"node", b"nodejs"} and any(
                token.rsplit(b"/", 1)[-1] in {b"claude", b"claude-code"}
                for token in command_tokens[1:]
            )
            if first in {b"claude", b"claude-code"} or node_launcher:
                matches.append(command)
        return ProcessState.RUNNING if matches else ProcessState.NOT_RUNNING

    def inspect(self) -> ObservedConfig:
        settings, raw, reasons = self._read_settings()
        if settings is None:
            return ObservedConfig(ConfigState.UNKNOWN, None, None, None, tuple(reasons))
        env = settings.get("env", {})
        if env is not None and not isinstance(env, dict):
            return ObservedConfig(
                ConfigState.UNKNOWN, None, None, None, tuple(reasons + ["env must be an object"])
            )
        env = env or {}
        base_url = env.get("ANTHROPIC_BASE_URL")
        token = env.get("ANTHROPIC_AUTH_TOKEN")
        if base_url is not None and not isinstance(base_url, str):
            return ObservedConfig(
                ConfigState.UNKNOWN, None, None, None, ("base URL must be a string",)
            )
        if token is not None and not isinstance(token, str):
            return ObservedConfig(
                ConfigState.UNKNOWN, None, None, None, ("auth token must be a string",)
            )

        override = self._environment_override(env, reasons)
        if override:
            state = ConfigState.EXTERNAL_OVERRIDE
            reasons.extend(override)
        elif base_url and base_url.rstrip("/") not in OFFICIAL_BASE_URLS:
            if not token:
                return ObservedConfig(
                    ConfigState.UNKNOWN,
                    None,
                    base_url,
                    None,
                    tuple(reasons + ["custom base URL requires an auth token"]),
                )
            state = ConfigState.CUSTOM
        elif token:
            state = ConfigState.OFFICIAL_API_KEY
        else:
            state = ConfigState.OFFICIAL_LOGIN
        fingerprint = hashlib.sha256(
            json.dumps({"base_url": base_url, "token": token}, sort_keys=True).encode()
        ).hexdigest()
        provider_id = None
        if state is ConfigState.CUSTOM and self.registry is not None:
            matches = [
                name
                for name, provider in self.registry.providers.items()
                if (binding := provider.clients.get("claude")) is not None
                and binding.base_url == base_url
                and binding.api_key == token
            ]
            if len(matches) > 1:
                return ObservedConfig(
                    ConfigState.UNKNOWN,
                    None,
                    base_url,
                    fingerprint,
                    tuple(reasons + ["custom Provider binding is ambiguous"]),
                )
            if matches:
                provider_id = matches[0]
        return ObservedConfig(state, provider_id, base_url, fingerprint, tuple(reasons))

    def prepare_provider(self, binding: Mapping[str, object]) -> PreparedChange:
        base_url = binding.get("base_url")
        api_key = binding.get("api_key")
        if not isinstance(base_url, str) or not isinstance(api_key, str) or not api_key:
            raise ValidationError("Claude binding requires base_url and api_key")
        settings, raw, _ = self._read_settings()
        if settings is None:
            settings = {}
            raw = b"{}\n"
        env = settings.get("env", {})
        if env is not None and not isinstance(env, dict):
            raise ValidationError("Claude settings env must be an object")
        updated = deepcopy(settings)
        updated.setdefault("env", {})
        updated["env"]["ANTHROPIC_BASE_URL"] = base_url
        updated["env"]["ANTHROPIC_AUTH_TOKEN"] = api_key
        after = json.dumps(updated, ensure_ascii=True, indent=2, sort_keys=True).encode() + b"\n"
        detected = self.inspect()
        return PreparedChange(
            "claude",
            (config_file(self.settings_path, raw),),
            (config_file(self.settings_path, after),),
            detected,
        )

    def prepare_snapshot_restore(self, manifest: Mapping[str, object]) -> PreparedChange:
        before = self._read_settings()[1]
        backup = _snapshot_file(manifest, "claude/settings.json", self.home / ".codelux")
        return PreparedChange(
            "claude",
            (config_file(self.settings_path, before),),
            (config_file(self.settings_path, backup),),
            self.inspect(),
        )

    def prepare_native_official_login(self) -> PreparedChange:
        """Remove only Codelux-owned routing fields before native Claude login."""
        settings, raw, _ = self._read_settings()
        if settings is None:
            raise ValidationError("Claude settings are invalid")
        env = settings.get("env", {})
        if env is not None and not isinstance(env, dict):
            raise ValidationError("Claude settings env must be an object")
        updated = deepcopy(settings)
        updated_env = updated.setdefault("env", {})
        updated_env.pop("ANTHROPIC_BASE_URL", None)
        updated_env.pop("ANTHROPIC_AUTH_TOKEN", None)
        after = json.dumps(updated, ensure_ascii=True, indent=2, sort_keys=True).encode() + b"\n"
        return PreparedChange(
            "claude",
            (config_file(self.settings_path, raw),),
            (config_file(self.settings_path, after),),
            self.inspect(),
        )

    def validate_files(self, files: Tuple[ConfigFile, ...]) -> None:
        if len(files) != 1 or files[0].path != self.settings_path:
            raise ValidationError("Claude change must contain settings.json only")
        try:
            parsed = json.loads(files[0].content)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValidationError("candidate Claude settings are invalid JSON") from exc
        if not isinstance(parsed, dict):
            raise ValidationError("Claude settings root must be an object")
        env = parsed.get("env", {})
        if env is not None and not isinstance(env, dict):
            raise ValidationError("Claude settings env must be an object")
        for key in ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN"):
            if key in (env or {}) and not isinstance((env or {})[key], str):
                raise ValidationError(f"Claude {key} must be a string")

    def commit(self, change: PreparedChange) -> None:
        self.validate_files(change.after)
        atomic_write_private(self.settings_path, change.after[0].content, self.config_root)

    def rollback(self, change: PreparedChange) -> None:
        self.validate_files(change.before)
        atomic_write_private(self.settings_path, change.before[0].content, self.config_root)

    def _read_settings(self) -> Tuple[Optional[dict], bytes, list]:
        if not self.settings_path.exists():
            return {}, b"{}\n", ["settings file is absent; treating as official login"]
        try:
            raw = self.settings_path.read_bytes()
            parsed = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            return None, b"", [f"settings unreadable: {type(exc).__name__}"]
        if not isinstance(parsed, dict):
            return None, raw, ["settings root must be an object"]
        return parsed, raw, []

    def _environment_override(self, env: Mapping[str, object], reasons: list) -> list:
        conflicts = []
        for key in ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN"):
            if key not in os.environ:
                continue
            value = os.environ[key]
            if key not in env:
                conflicts.append(f"{key} is externally set but absent from settings")
            elif env[key] != value:
                conflicts.append(f"{key} differs from settings")
            else:
                reasons.append(f"{key} externally set with matching value")
        return conflicts


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
    raise ValidationError("snapshot does not contain the Claude settings file")
