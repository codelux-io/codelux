"""Fixed-command OpenSSH transport for Phase C synchronization."""

import io
import json
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from codelux import __version__
from codelux.errors import ValidationError
from codelux.sync import (
    MAX_FILE,
    MAX_FILES,
    MAX_TOTAL,
    SyncManifest,
    machine_id,
    parse_plain_archive,
)

MAX_CAPABILITY_LINE = 16 * 1024
SSH_OPTIONS = (
    "-o",
    "ConnectTimeout=10",
    "-o",
    "ServerAliveInterval=30",
    "-o",
    "ServerAliveCountMax=10",
)


@dataclass(frozen=True)
class Capability:
    protocol_versions: tuple[int, ...]
    supported_selections: tuple[str, ...]
    installed_clients: tuple[str, ...]
    max_file_size: int
    max_file_count: int
    max_total_size: int
    supports_keys: bool
    machine_id: str
    codelux_version: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_versions": list(self.protocol_versions),
            "supported_selections": list(self.supported_selections),
            "installed_clients": list(self.installed_clients),
            "max_file_size": self.max_file_size,
            "max_file_count": self.max_file_count,
            "max_total_size": self.max_total_size,
            "supports_keys": self.supports_keys,
            "machine_id": self.machine_id,
            "codelux_version": self.codelux_version,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> "Capability":
        required = {
            "protocol_versions",
            "supported_selections",
            "installed_clients",
            "max_file_size",
            "max_file_count",
            "max_total_size",
            "supports_keys",
            "machine_id",
            "codelux_version",
        }
        if not isinstance(raw, dict) or set(raw) != required:
            raise ValidationError("remote sync capability is invalid")
        try:
            result = cls(
                tuple(int(item) for item in raw["protocol_versions"]),
                tuple(str(item) for item in raw["supported_selections"]),
                tuple(str(item) for item in raw["installed_clients"]),
                int(raw["max_file_size"]),
                int(raw["max_file_count"]),
                int(raw["max_total_size"]),
                raw["supports_keys"],
                str(raw["machine_id"]),
                str(raw["codelux_version"]),
            )
        except (TypeError, ValueError) as exc:
            raise ValidationError("remote sync capability is invalid") from exc
        if not isinstance(result.supports_keys, bool) or not result.machine_id:
            raise ValidationError("remote sync capability is invalid")
        return result


def local_capability(home: Path) -> Capability:
    installed = tuple(
        name
        for name, path in (("claude", home / ".claude"), ("codex", home / ".codex"))
        if path.is_dir() and not path.is_symlink()
    )
    return Capability(
        (1,),
        ("config", "providers", "sessions"),
        installed,
        MAX_FILE,
        MAX_FILES,
        MAX_TOTAL,
        True,
        machine_id(home / ".codelux"),
        __version__,
    )


def canonical_line(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode() + b"\n"


def read_capability(stream: Any) -> Capability:
    line = stream.readline(MAX_CAPABILITY_LINE + 1)
    if not line or len(line) > MAX_CAPABILITY_LINE or not line.endswith(b"\n"):
        raise ValidationError("remote sync capability is missing or too large")
    try:
        return Capability.from_dict(json.loads(line))
    except json.JSONDecodeError as exc:
        raise ValidationError("remote sync capability is invalid") from exc


def validate_capability(remote: Capability, manifest: SyncManifest) -> None:
    if 1 not in remote.protocol_versions:
        raise ValidationError("remote sync protocol is incompatible; upgrade Codelux")
    missing = set(manifest.selection).difference(remote.supported_selections)
    if missing:
        raise ValidationError("remote does not support selections: " + ", ".join(sorted(missing)))
    required_clients = {
        path.split("/", 1)[0]
        for path in (item.path for item in manifest.files)
        if path.startswith(("claude/", "codex/"))
    }
    unavailable = required_clients.difference(remote.installed_clients)
    if unavailable:
        raise ValidationError("remote clients are not installed: " + ", ".join(sorted(unavailable)))
    if manifest.includes_keys and not remote.supports_keys:
        raise ValidationError("remote does not support credential transfer")
    if len(manifest.files) > remote.max_file_count:
        raise ValidationError("sync exceeds remote file count limit")
    if any(item.size > remote.max_file_size for item in manifest.files):
        raise ValidationError("sync exceeds remote per-file limit")
    if sum(item.size for item in manifest.files) > remote.max_total_size:
        raise ValidationError("sync exceeds remote total-size limit")


def ssh_command(target: str, remote_args: Sequence[str]) -> list[str]:
    if (
        not target
        or target.startswith("-")
        or any(char.isspace() or ord(char) < 32 for char in target)
    ):
        raise ValidationError("SSH target is invalid")
    if any(
        not value or any(char.isspace() or ord(char) < 32 or char in "'\";|&$`" for char in value)
        for value in remote_args
    ):
        raise ValidationError("SSH transport argument is invalid")
    return ["ssh", *SSH_OPTIONS, "--", target, "codelux", "sync", "transport", *remote_args]


def push_archive(
    target: str,
    manifest: SyncManifest,
    archive: bytes,
    overwrite: bool,
    claude_project_root: Optional[str] = None,
    progress: Optional[Callable[[str], None]] = None,
    overwrite_clients: Sequence[str] = (),
) -> tuple[Capability, dict[str, Any]]:
    args = ["receive", "--protocol", "1"]
    if overwrite:
        args.append("--overwrite")
    for client in sorted(set(overwrite_clients)):
        if client not in {"claude", "codex"}:  # pragma: no cover - validated internal input
            raise ValidationError("unsupported overwrite client")
        args.append(f"--overwrite-{client}")  # pragma: no cover - validated internal input
    if claude_project_root:
        args.extend(["--claude-project-root", claude_project_root])
    process = subprocess.Popen(
        ssh_command(target, args),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdin is not None and process.stdout is not None
    try:
        if progress:
            progress("Opening SSH connection...")
        capability = read_capability(process.stdout)
        if progress:
            progress("Remote capability check passed; sending archive...")
        validate_capability(capability, manifest)
        process.stdin.write(archive)
        process.stdin.close()
        if progress:
            progress("Archive sent; waiting for target commit...")
        response_raw = process.stdout.readline(MAX_CAPABILITY_LINE + 1)
        stderr = process.stderr.read() if process.stderr is not None else b""
        code = process.wait()
    except Exception:
        process.kill()
        process.wait()
        raise
    if code != 0:
        raise ValidationError(
            "remote sync failed"
            + (": " + stderr.decode(errors="replace").strip() if stderr else "")
        )
    try:
        response = json.loads(response_raw)
    except json.JSONDecodeError as exc:
        raise ValidationError("remote sync response is invalid") from exc
    if not isinstance(response, dict) or response.get("status") != "committed":
        raise ValidationError("remote sync did not confirm commit")
    return capability, response


def pull_archive(
    target: str,
    home: Path,
    selection: Sequence[str],
    include_keys: bool,
    progress: Optional[Callable[[str], None]] = None,
    clients: Sequence[str] = (),
) -> tuple[Capability, SyncManifest, dict[str, bytes]]:
    selected = tuple(sorted(set(selection)))
    if not selected or set(selected).difference({"config", "providers", "sessions"}):
        raise ValidationError("at least one supported sync selection is required")
    args = ["send", "--protocol", "1"]
    args.extend(f"--{name}" for name in selected)
    args.append("--keys" if include_keys else "--no-keys")
    for client in sorted(set(clients)):
        if client not in {"claude", "codex"}:  # pragma: no cover - validated internal input
            raise ValidationError("unsupported sync client")
        args.append(f"--{client}-sessions")
    if progress:
        progress("Opening SSH connection and requesting archive...")
    process = subprocess.run(
        ssh_command(target, args),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if process.returncode != 0:
        stderr = process.stderr.decode(errors="replace").strip()
        raise ValidationError("remote sync failed" + (": " + stderr if stderr else ""))
    stream = io.BytesIO(process.stdout)
    capability = read_capability(stream)
    manifest, files = parse_plain_archive(stream.read())
    if manifest.selection != selected:
        raise ValidationError("remote sync returned an unexpected selection")
    if manifest.includes_keys is not include_keys:
        raise ValidationError("remote sync returned an unexpected credential policy")
    validate_capability(capability, manifest)
    validate_capability(local_capability(home), manifest)
    if progress:
        progress("Remote archive received and validated...")
    return capability, manifest, files


def decode_transport_archive(raw: bytes) -> tuple[SyncManifest, dict[str, bytes]]:
    return parse_plain_archive(raw)
