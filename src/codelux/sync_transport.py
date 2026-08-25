"""Fixed-command OpenSSH transport for Phase C synchronization."""

import io
import errno
import json
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Callable, Optional, Union, cast

from codelux import __version__
from codelux.client_paths import claude_config_root
from codelux.errors import ValidationError
from codelux.sync import (
    MAX_FILE,
    MAX_FILES,
    MAX_TOTAL,
    OVERWRITE_SCOPES,
    SELECTIONS,
    SyncManifest,
    machine_id,
    parse_plain_archive,
    parse_plain_archive_stream,
)

MAX_CAPABILITY_LINE = 16 * 1024
MAX_PREFLIGHT_LINE = 16 * 1024 * 1024
MAX_REMOTE_ERROR = 64 * 1024
ARCHIVE_CHUNK = 1024 * 1024
ARCHIVE_SPOOL_MEMORY = 8 * 1024 * 1024
MAX_ARCHIVE_SIZE = MAX_TOTAL + (MAX_FILES + 1) * 1024
ArchiveSource = Union[bytes, BinaryIO]
SSH_OPTIONS = (
    "-o",
    "ConnectTimeout=10",
    "-o",
    "ServerAliveInterval=30",
    "-o",
    "ServerAliveCountMax=10",
)


class RemoteProjectDiscoveryUnavailable(ValidationError):
    """Raised when the remote cannot execute the optional discovery command."""


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
        for name, path in (("claude", claude_config_root(home)), ("codex", home / ".codex"))
        if path.is_dir() and not path.is_symlink()
    )
    return Capability(
        (1, 2),
        tuple(sorted(SELECTIONS)),
        installed,
        MAX_FILE,
        MAX_FILES,
        MAX_TOTAL,
        True,
        machine_id(home / ".codelux"),
        __version__,
    )


def canonical_line(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode() + b"\n"


def read_capability(stream: Any) -> Capability:
    line = stream.readline(MAX_CAPABILITY_LINE + 1)
    if not line or len(line) > MAX_CAPABILITY_LINE or not line.endswith(b"\n"):
        raise ValidationError("remote sync capability is missing or too large")
    try:
        return Capability.from_dict(json.loads(line))
    except json.JSONDecodeError as exc:
        raise ValidationError("remote sync capability is invalid") from exc


def validate_capability(remote: Capability, manifest: SyncManifest, protocol: int = 1) -> None:
    if protocol not in remote.protocol_versions:
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


def read_path_payload(stream: Any) -> Any:
    line = stream.readline(MAX_CAPABILITY_LINE + 1)
    if not line or len(line) > MAX_CAPABILITY_LINE or not line.endswith(b"\n"):
        raise ValidationError("project path payload is missing or too large")
    try:
        return json.loads(line)
    except json.JSONDecodeError as exc:
        raise ValidationError("project path payload is invalid") from exc


def preflight_payload(
    manifest: SyncManifest,
    archive_size: int,
    environment_project_roots: Optional[Mapping[str, Path]] = None,
    session_project_roots: Sequence[Path] = (),
) -> bytes:
    if archive_size < 0 or archive_size > MAX_ARCHIVE_SIZE:
        raise ValidationError("sync archive exceeds total size limit")
    return canonical_line(
        {
            "manifest": manifest.to_dict(),
            "archive_size": archive_size,
            "environment_project_roots": {
                key: str(path.expanduser().absolute())
                for key, path in (environment_project_roots or {}).items()
            },
            "session_project_roots": [
                str(path.expanduser().absolute()) for path in session_project_roots
            ],
        }
    )


def read_preflight_payload(
    stream: Any,
) -> tuple[SyncManifest, int, dict[str, str], tuple[str, ...]]:
    line = stream.readline(MAX_PREFLIGHT_LINE + 1)
    if not line or len(line) > MAX_PREFLIGHT_LINE or not line.endswith(b"\n"):
        raise ValidationError("sync preflight payload is missing or too large")
    try:
        raw = json.loads(line)
        if not isinstance(raw, dict) or set(raw) != {
            "manifest",
            "archive_size",
            "environment_project_roots",
            "session_project_roots",
        }:
            raise ValueError
        manifest = SyncManifest.from_dict(raw["manifest"])
        archive_size = int(raw["archive_size"])
        mappings = raw["environment_project_roots"]
        sessions = raw["session_project_roots"]
        if (
            isinstance(raw["archive_size"], bool)
            or archive_size < 0
            or archive_size > MAX_ARCHIVE_SIZE
            or not isinstance(mappings, dict)
            or any(
                not isinstance(key, str) or not isinstance(value, str)
                for key, value in mappings.items()
            )
            or not isinstance(sessions, list)
            or any(not isinstance(item, str) for item in sessions)
        ):
            raise ValueError
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValidationError("sync preflight payload is invalid") from exc
    return manifest, archive_size, dict(mappings), tuple(sessions)


def receive_archive_stream(
    stream: BinaryIO, declared_size: Optional[int] = None
) -> tuple[SyncManifest, dict[str, bytes]]:
    with tempfile.SpooledTemporaryFile(max_size=ARCHIVE_SPOOL_MEMORY) as archive:
        total = 0
        while True:
            chunk = stream.read(ARCHIVE_CHUNK)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_ARCHIVE_SIZE or (declared_size is not None and total > declared_size):
                raise ValidationError("sync archive exceeds declared size")
            archive.write(chunk)
        if declared_size is not None and total != declared_size:
            raise ValidationError("sync archive size does not match preflight")
        return parse_plain_archive_stream(cast(BinaryIO, archive), total)


def discover_remote_project_candidates(target: str) -> tuple[Path, ...]:
    process = subprocess.run(
        ssh_command(target, ["discover-projects", "--protocol", "1"]),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if process.returncode != 0:
        stderr = process.stderr.decode(errors="replace").strip()
        raise RemoteProjectDiscoveryUnavailable(
            "remote project discovery failed" + (": " + stderr if stderr else "")
        )
    stream = io.BytesIO(process.stdout)
    decoded = read_path_payload(stream)
    if stream.read() or not isinstance(decoded, list):
        raise ValidationError("remote project discovery response is invalid")
    candidates = []
    for value in decoded:
        if (
            not isinstance(value, str)
            or not value
            or any(ord(character) < 32 for character in value)
        ):
            raise ValidationError("remote project discovery response is invalid")
        path = Path(value)
        if not path.is_absolute() or path in candidates:
            raise ValidationError("remote project discovery response is invalid")
        candidates.append(path)
    return tuple(candidates)


class _Protocol2Unavailable(Exception):
    pass


def _receive_args(
    protocol: int,
    overwrite: bool,
    claude_project_root: Optional[str],
    overwrite_clients: Sequence[str],
    overwrite_scopes: Sequence[str],
    include_legacy_mapping: bool,
) -> list[str]:
    args = ["receive", "--protocol", str(protocol)]
    if overwrite:
        args.append("--overwrite")
    for client in sorted(set(overwrite_clients)):
        if client not in {"claude", "codex"}:  # pragma: no cover - validated internal input
            raise ValidationError("unsupported overwrite client")
        args.append(f"--overwrite-{client}")  # pragma: no cover - validated internal input
    for scope in sorted(set(overwrite_scopes)):
        if scope not in OVERWRITE_SCOPES:
            raise ValidationError("unsupported overwrite scope")
        args.extend(["--overwrite-scope", scope])
    if claude_project_root:
        args.extend(["--claude-project-root", claude_project_root])
    if include_legacy_mapping:
        args.append("--project-map-stdin")
    return args


def _archive_size(archive: ArchiveSource) -> int:
    if isinstance(archive, bytes):
        return len(archive)
    try:
        archive.seek(0, io.SEEK_END)
        size = archive.tell()
        archive.seek(0)
    except (AttributeError, OSError, ValueError) as exc:
        raise ValidationError("sync archive stream must be seekable") from exc
    if size < 0 or size > MAX_ARCHIVE_SIZE:
        raise ValidationError("sync archive exceeds total size limit")
    return size


def _remote_stderr(process: Any) -> str:
    if process.stderr is None:
        return ""
    raw = bytes(process.stderr.read(MAX_REMOTE_ERROR + 1))
    if len(raw) > MAX_REMOTE_ERROR:
        raw = raw[:MAX_REMOTE_ERROR] + b"\n[remote error truncated]"
    return raw.decode(errors="replace").strip()


def _raise_broken_pipe(process: Any) -> None:
    process.kill()
    process.wait()
    stderr = _remote_stderr(process)
    raise ValidationError(
        "remote sync connection closed while receiving the archive"
        + (": " + stderr if stderr else "")
    )


def _send_archive(
    process: Any,
    archive: ArchiveSource,
    archive_size: int,
    progress: Optional[Callable[[str], None]],
) -> None:
    assert process.stdin is not None
    sent = 0
    next_report = 25
    try:
        if isinstance(archive, bytes):
            source: BinaryIO = io.BytesIO(archive)
        else:
            archive.seek(0)
            source = archive
        while True:
            chunk = source.read(ARCHIVE_CHUNK)
            if not chunk:
                break
            process.stdin.write(chunk)
            sent += len(chunk)
            percent = 100 if archive_size == 0 else int(sent * 100 / archive_size)
            if progress and archive_size >= 4 * ARCHIVE_CHUNK and percent >= next_report:
                progress(f"Archive upload {min(percent, 100)}% ({sent}/{archive_size} bytes)...")
                next_report += 25
        process.stdin.close()
    except OSError as exc:
        if exc.errno != errno.EPIPE:
            raise
        _raise_broken_pipe(process)
    if sent != archive_size:
        process.kill()
        process.wait()
        raise ValidationError("sync archive stream size changed during transfer")


def _read_commit_response(
    process: Any,
) -> dict[str, Any]:
    assert process.stdout is not None
    response_raw = process.stdout.readline(MAX_CAPABILITY_LINE + 1)
    stderr = _remote_stderr(process)
    code = process.wait()
    if code != 0:
        raise ValidationError("remote sync failed" + (": " + stderr if stderr else ""))
    if not response_raw or len(response_raw) > MAX_CAPABILITY_LINE:
        raise ValidationError("remote sync response is invalid")
    try:
        response = json.loads(response_raw)
    except json.JSONDecodeError as exc:
        raise ValidationError("remote sync response is invalid") from exc
    if not isinstance(response, dict) or response.get("status") != "committed":
        raise ValidationError("remote sync did not confirm commit")
    return response


def _push_archive_protocol(
    target: str,
    manifest: SyncManifest,
    archive: ArchiveSource,
    archive_size: int,
    overwrite: bool,
    protocol: int,
    claude_project_root: Optional[str],
    progress: Optional[Callable[[str], None]],
    overwrite_clients: Sequence[str],
    overwrite_scopes: Sequence[str],
    environment_project_roots: Optional[Mapping[str, Path]],
    session_project_roots: Sequence[Path],
) -> tuple[Capability, dict[str, Any]]:
    legacy_mapping = protocol == 1 and bool(environment_project_roots)
    args = _receive_args(
        protocol,
        overwrite,
        claude_project_root,
        overwrite_clients,
        overwrite_scopes,
        legacy_mapping,
    )
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
        try:
            capability = read_capability(process.stdout)
        except ValidationError as exc:
            process.wait()
            stderr = _remote_stderr(process)
            if protocol == 2 and "unsupported sync protocol" in stderr:
                raise _Protocol2Unavailable from exc
            raise ValidationError(str(exc) + (": " + stderr if stderr else "")) from exc
        if progress:
            progress(f"Remote capability check passed for protocol {protocol}.")
        validate_capability(capability, manifest, protocol)
        if protocol == 2:
            try:
                process.stdin.write(
                    preflight_payload(
                        manifest,
                        archive_size,
                        environment_project_roots,
                        session_project_roots,
                    )
                )
                process.stdin.flush()
            except OSError as exc:
                if exc.errno != errno.EPIPE:
                    raise
                _raise_broken_pipe(process)
            ready_raw = process.stdout.readline(MAX_CAPABILITY_LINE + 1)
            if not ready_raw or len(ready_raw) > MAX_CAPABILITY_LINE:
                process.wait()
                stderr = _remote_stderr(process)
                raise ValidationError(
                    "remote sync preflight failed" + (": " + stderr if stderr else "")
                )
            try:
                ready = json.loads(ready_raw)
            except json.JSONDecodeError as exc:
                raise ValidationError("remote sync preflight response is invalid") from exc
            if not isinstance(ready, dict) or ready.get("status") != "ready":
                raise ValidationError("remote sync preflight was not accepted")
            if progress:
                progress("Remote preflight passed; sending archive...")
        else:
            if environment_project_roots:
                process.stdin.write(
                    canonical_line(
                        {
                            key: str(path.expanduser().absolute())
                            for key, path in environment_project_roots.items()
                        }
                    )
                )
            if progress:
                progress("Legacy remote accepted capability; sending archive...")
        _send_archive(process, archive, archive_size, progress)
        if progress:
            progress("Archive sent; waiting for target commit...")
        response = _read_commit_response(process)
    except Exception:
        if process.poll() is None:
            process.kill()
            process.wait()
        raise
    return capability, response


def push_archive(
    target: str,
    manifest: SyncManifest,
    archive: ArchiveSource,
    overwrite: bool,
    claude_project_root: Optional[str] = None,
    progress: Optional[Callable[[str], None]] = None,
    overwrite_clients: Sequence[str] = (),
    overwrite_scopes: Sequence[str] = (),
    environment_project_roots: Optional[Mapping[str, Path]] = None,
    session_project_roots: Sequence[Path] = (),
) -> tuple[Capability, dict[str, Any]]:
    archive_size = _archive_size(archive)
    try:
        return _push_archive_protocol(
            target,
            manifest,
            archive,
            archive_size,
            overwrite,
            2,
            claude_project_root,
            progress,
            overwrite_clients,
            overwrite_scopes,
            environment_project_roots,
            session_project_roots,
        )
    except _Protocol2Unavailable:
        if progress:
            progress("Remote does not support protocol 2; retrying with legacy protocol 1...")
        return _push_archive_protocol(
            target,
            manifest,
            archive,
            archive_size,
            overwrite,
            1,
            claude_project_root,
            progress,
            overwrite_clients,
            overwrite_scopes,
            environment_project_roots,
            session_project_roots,
        )


def pull_archive(
    target: str,
    home: Path,
    selection: Sequence[str],
    include_keys: bool,
    progress: Optional[Callable[[str], None]] = None,
    clients: Sequence[str] = (),
    project_roots: Sequence[Path] = (),
) -> tuple[Capability, SyncManifest, dict[str, bytes]]:
    selected = tuple(sorted(set(selection)))
    if not selected or set(selected).difference(SELECTIONS):
        raise ValidationError("at least one supported sync selection is required")
    args = ["send", "--protocol", "1"]
    args.extend(f"--{name.replace('_', '-')}" for name in selected)
    args.append("--keys" if include_keys else "--no-keys")
    for client in sorted(set(clients)):
        if client not in {"claude", "codex"}:  # pragma: no cover - validated internal input
            raise ValidationError("unsupported sync client")
        args.append(f"--{client}-sessions")
    mapping_payload = None
    if project_roots:
        args.append("--project-roots-stdin")
        mapping_payload = canonical_line(
            [str(path.expanduser().absolute()) for path in project_roots]
        )
    if progress:
        progress("Opening SSH connection and requesting archive...")
    process = subprocess.run(
        ssh_command(target, args),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        input=mapping_payload,
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
