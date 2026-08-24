"""CLI entry point for the local Provider management MVP."""

import json
import os
import sys
from functools import wraps
from pathlib import Path
from typing import Any, Callable, Optional, TypedDict, TypeVar

import click

from codelux import __version__
from codelux.adapters import ClaudeAdapter, CodexAdapter
from codelux.coordinator import TransactionCoordinator
from codelux.errors import CodeluxError, RecoveryRequiredError, ValidationError
from codelux.locking import OperationLock
from codelux.models import (
    ConfigState,
    FileState,
    HealthState,
    Manifest,
    OperationState,
    ProcessState,
)
from codelux.registry import (
    SUPPORTED_CLIENTS,
    ClientBinding,
    ProviderRecord,
    Registry,
    validate_provider_name,
)
from codelux.registry_io import load_registry, save_registry
from codelux.safe_files import atomic_write_private
from codelux.snapshots import SnapshotStore
from codelux.sync import (
    _project_id,
    apply_import,
    build_manifest,
    codex_session_projects,
    create_plain_archive,
    export_encrypted,
    import_encrypted,
    load_sync_state,
    map_claude_sessions,
    map_codex_sessions,
    materialize_sync_files,
    reset_baseline,
    rotate_machine_id,
    select_claude_project,
    select_claude_projects,
    select_codex_projects,
)
from codelux.sync_transport import (
    canonical_line,
    local_capability,
    parse_plain_archive,
    pull_archive,
    push_archive,
    read_path_payload,
    validate_capability,
)

F = TypeVar("F", bound=Callable[..., None])


class ProviderListRow(TypedDict):
    name: str
    clients: list[str]
    description: str
    builtin: bool


@click.group(
    name="codelux",
    help="Unified Provider management CLI for AI coding assistants",
)
@click.version_option(version=__version__, prog_name="codelux")
def main() -> None:
    """Main CLI entry point."""


@main.command()
def version() -> None:
    """Display version information."""
    click.echo(f"codelux version {__version__}")


@main.group(name="sync")
def sync_group() -> None:
    """Synchronize selected state between isolated Codelux homes."""


def _sync_selection(
    config: bool,
    sessions: bool,
    providers: bool,
    project_env: bool = False,
    local_env: bool = False,
    user_env: bool = False,
    memory: bool = False,
) -> tuple[str, ...]:
    selected = tuple(
        name
        for name, enabled in (
            ("config", config),
            ("sessions", sessions),
            ("providers", providers),
            ("project_env", project_env),
            ("local_env", local_env),
            ("user_env", user_env),
            ("memory", memory),
        )
        if enabled
    )
    if not selected:
        raise ValidationError("select at least one supported synchronization scope")
    if local_env and not project_env:
        raise ValidationError("--local-project-env requires --project-env")
    return selected


def _environment_project_roots(
    values: tuple[Path, ...], *, local: bool, prompt: str, default_cwd: bool = False
) -> tuple[Path, ...]:
    if values:
        return tuple(_project_directory(value, local=local) for value in values)
    default = str(Path.cwd()) if default_cwd else ""
    value = click.prompt(prompt, default=default, show_default=default_cwd)
    return (_project_directory(value, local=local),)


def _environment_target_mapping(
    manifest: object, source_roots: tuple[Path, ...], target_roots: tuple[Path, ...]
) -> dict[str, Path]:
    project_ids = tuple(_project_id(path) for path in source_roots)
    if set(getattr(manifest, "project_ids", ())) != set(project_ids):
        raise ValidationError("project environment source mapping does not match the archive")
    if len(source_roots) != len(target_roots):
        raise ValidationError("each source project requires one target project directory")
    return dict(zip(project_ids, target_roots))


def _explicit_project_mappings(
    values: tuple[str, ...], *, source_local: bool, target_local: bool
) -> tuple[tuple[Path, ...], dict[str, Path]]:
    sources = []
    mapping: dict[str, Path] = {}
    for value in values:
        if "=" not in value:
            raise ValidationError("--project-map must use SOURCE=TARGET")
        source_text, target_text = value.split("=", 1)
        source = _project_directory(source_text, local=source_local)
        target = _project_directory(target_text, local=target_local)
        project_id = _project_id(source)
        if project_id in mapping or target in mapping.values():
            raise ValidationError("project mappings must use distinct sources and targets")
        sources.append(source)
        mapping[project_id] = target
    return tuple(sources), mapping


def _target_mapping_by_id(project_ids: tuple[str, ...], values: tuple[str, ...]) -> dict[str, Path]:
    mapping: dict[str, Path] = {}
    for value in values:
        if "=" not in value:
            raise ValidationError("--target-project must use PROJECT_ID=TARGET")
        project_id, target_text = value.split("=", 1)
        if project_id not in project_ids or project_id in mapping:
            raise ValidationError(
                "target project mapping contains an unknown or duplicate project ID"
            )
        target = _project_directory(target_text, local=True)
        if target in mapping.values():
            raise ValidationError("project mappings must use distinct targets")
        mapping[project_id] = target
    if set(mapping) != set(project_ids):
        raise ValidationError("each project ID requires one target project directory")
    return mapping


def _environment_targets_for_sources(
    source_roots: tuple[Path, ...], values: tuple[Path, ...], *, local: bool
) -> tuple[Path, ...]:
    if values:
        targets = tuple(_project_directory(value, local=local) for value in values)
    else:
        targets = tuple(
            _project_directory(
                click.prompt(f"Target project directory for agent environment {source}"),
                local=local,
            )
            for source in source_roots
        )
    if len(targets) != len(source_roots):
        raise ValidationError("each source project requires one target project directory")
    return targets


def _sync_password(password_stdin: bool) -> str:
    if password_stdin:
        value = sys.stdin.readline().rstrip("\n")
    else:
        value = str(click.prompt("Sync password", hide_input=True, confirmation_prompt=True))
    if len(value) < 12:
        raise ValidationError("sync password must contain at least 12 characters")
    return value


def _sync_locked(command: F) -> F:
    @wraps(command)
    def wrapper(*args: object, **kwargs: object) -> None:
        root = _home() / ".codelux"
        with OperationLock(root / "operation.lock"):
            recovery = root / "recovery.json"
            if recovery.is_file() or recovery.is_symlink():
                raise click.ClickException("recovery is required before another write operation")
            incomplete = SnapshotStore(root).latest_incomplete()
            if incomplete is not None:
                SnapshotStore(root).require_recovery(incomplete)
                raise click.ClickException(
                    f"incomplete operation {incomplete.operation_id} requires recovery"
                )
            command(*args, **kwargs)

    return wrapper  # type: ignore[return-value]


@sync_group.command(name="export")
@click.option("--output", type=click.Path(path_type=Path), required=True)
@click.option("--config", "include_config", is_flag=True)
@click.option("--sessions", "include_sessions", is_flag=True)
@click.option("--providers", "include_providers", is_flag=True)
@click.option("--project-env", is_flag=True, help="Synchronize shared project agent files.")
@click.option("--local-project-env", is_flag=True, help="Include local project overrides.")
@click.option("--user-env", is_flag=True, help="Synchronize user-level agent configuration.")
@click.option("--memory", "include_memory", is_flag=True, help="Synchronize project memory.")
@click.option("--project-root", multiple=True, type=click.Path(path_type=Path))
@click.option(
    "--keys/--no-keys",
    default=True,
    help="Compatibility option; official account files are never synchronized.",
)
@click.option(
    "--confirm-keys",
    is_flag=True,
    help="Compatibility option; official account files are never synchronized.",
)
@click.option("--password-stdin", is_flag=True)
@_sync_locked
def sync_export(
    output: Path,
    include_config: bool,
    include_sessions: bool,
    include_providers: bool,
    project_env: bool,
    local_project_env: bool,
    user_env: bool,
    include_memory: bool,
    project_root: tuple[Path, ...],
    keys: bool,
    confirm_keys: bool,
    password_stdin: bool,
) -> None:
    """Create an authenticated encrypted offline sync archive."""
    try:
        selected = _sync_selection(
            include_config,
            include_sessions,
            include_providers,
            project_env,
            local_project_env,
            user_env,
            include_memory,
        )
        roots = (
            _environment_project_roots(
                project_root, local=True, prompt="Source project directory", default_cwd=True
            )
            if set(selected).intersection({"project_env", "local_env", "memory"})
            else ()
        )
        _sync_process_preflight(selected)
        manifest, files = build_manifest(_home(), selected, keys, project_roots=roots)
        export_encrypted(manifest, files, _sync_password(password_stdin), output)
        click.echo(f"exported {manifest.transfer_id}")
    except CodeluxError as exc:
        raise click.ClickException(str(exc)) from exc


def _push_selection(
    sessions: bool,
    providers: bool,
    claude_sessions: bool = False,
    codex_sessions: bool = False,
    overwrite: bool = False,
    project_env: bool = False,
    local_env: bool = False,
    user_env: bool = False,
    memory: bool = False,
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    requested_clients = []
    if sessions or claude_sessions:
        requested_clients.append("claude")
    if sessions or codex_sessions:
        requested_clients.append("codex")
    if local_env and not project_env:
        raise ValidationError("--local-project-env requires --project-env")
    explicit = providers or bool(requested_clients) or project_env or user_env or memory
    prompted = []
    if not explicit:
        if _safe_confirm("Synchronize third-party Providers and API keys?", default=True):
            providers = True
        if _safe_confirm("Synchronize Claude Code project history?", default=False):
            requested_clients.append("claude")
        if _safe_confirm("Synchronize Codex session history?", default=False):
            requested_clients.append("codex")
        project_env = _safe_confirm("Synchronize shared project agent environment?", default=False)
        if project_env:
            local_env = _safe_confirm("Include local project overrides?", default=False)
        user_env = _safe_confirm("Synchronize user-level agent environment?", default=False)
        memory = _safe_confirm("Synchronize project memory?", default=False)
    if providers:
        prompted.append("providers")
    if requested_clients:
        prompted.append("sessions")
    if project_env:
        prompted.append("project_env")
    if local_env:
        prompted.append("local_env")
    if user_env:
        prompted.append("user_env")
    if memory:
        prompted.append("memory")
    return tuple(prompted), tuple(sorted(set(requested_clients))), ()


def _safe_confirm(prompt: str, default: bool) -> bool:
    try:
        return click.confirm(prompt, default=default)
    except click.Abort:
        return default


def _session_overwrite_prompts(clients: tuple[str, ...], forced: bool) -> tuple[str, ...]:
    if forced:
        return clients
    approved = []
    for client in clients:
        label = "Claude Code project history" if client == "claude" else "Codex session history"
        allowed = _safe_confirm(f"Allow overwriting conflicting target {label}?", default=False)
        if allowed:
            approved.append(client)
    return tuple(approved)


def _project_directory(value: object, *, local: bool) -> Path:
    path = Path(str(value))
    if not path.is_absolute():
        raise ValidationError(
            "project directory must be a real absolute path; run pwd in the project "
            "on the target machine and paste its output"
        )
    path = Path(os.path.abspath(path))
    if local and (not path.is_dir() or path.is_symlink()):
        raise ValidationError(f"local project directory does not exist or is unsafe: {path}")
    return path


def _claude_source_project(files: dict[str, bytes], slug: str) -> Optional[str]:
    prefix = f"claude/projects/{slug}/"
    for logical, content in sorted(files.items()):
        if not logical.startswith(prefix) or not logical.endswith(".jsonl"):
            continue
        for raw_line in content.splitlines():
            try:
                record = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                cwd = record.get("cwd")
                if isinstance(cwd, str):
                    return cwd
    return None


def _session_project_directories(files: dict[str, bytes]) -> tuple[Path, ...]:
    roots = {
        Path(project)
        for logical in files
        if logical.startswith("claude/projects/")
        for slug in (logical.split("/", 3)[2],)
        for project in (_claude_source_project(files, slug),)
        if project is not None
    }
    roots.update(Path(project) for project in codex_session_projects(files))
    has_claude_history = any(path.startswith("claude/projects/") for path in files)
    if has_claude_history and not any(
        _claude_source_project(files, path.split("/", 3)[2]) is not None
        for path in files
        if path.startswith("claude/projects/")
    ):
        raise ValidationError("Claude session history does not identify a real project path")
    return tuple(sorted(roots, key=str))


def _claude_source_slug(files: dict[str, bytes], slugs: list[str], selected_path: str) -> str:
    source_path = _project_directory(selected_path, local=False)
    matches = [slug for slug in slugs if _claude_source_project(files, slug) == str(source_path)]
    if len(matches) != 1:
        raise ValidationError("unknown or ambiguous Claude source project path")
    return matches[0]


def _prompt_project_directory(label: str, *, local: bool) -> Optional[Path]:
    value = click.prompt(
        f"{label} (enter a real absolute path; Enter to skip)",
        default="",
        show_default=False,
    )
    return _project_directory(value, local=local) if value else None


def _codex_project_mapping(files: dict[str, bytes], *, local: bool = False) -> dict[str, Path]:
    projects = codex_session_projects(files)
    if not projects:
        return {}
    click.echo(
        "Codex session history is project-specific and must be mapped to target project directories."
    )
    mappings = {}
    for project in projects:
        target = _prompt_project_directory(
            f"Target project directory for Codex source project {project}", local=local
        )
        if target is not None:
            mappings[project] = target
    return mappings


@sync_group.command(name="import")
@click.argument("archive", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--password-stdin", is_flag=True)
@click.option("--overwrite", is_flag=True)
@click.option("--target-project-root", multiple=True, type=click.Path(path_type=Path))
@click.option(
    "--target-project",
    multiple=True,
    help="Explicit environment mapping as PROJECT_ID=TARGET; required for multiple projects.",
)
@click.option(
    "--claude-project-root",
    type=click.Path(path_type=Path),
    help="Target project root for Claude session history.",
)
@click.option(
    "--claude-source-project",
    help="Real absolute source Claude project path to map when syncing one project.",
)
@click.option(
    "--apply-active-provider",
    is_flag=True,
    help="Allow the archive to replace the target active client configuration.",
)
@_sync_locked
def sync_import(
    archive: Path,
    password_stdin: bool,
    overwrite: bool,
    target_project_root: tuple[Path, ...],
    target_project: tuple[str, ...],
    claude_project_root: Optional[Path],
    claude_source_project: Optional[str],
    apply_active_provider: bool,
) -> None:
    """Apply an encrypted archive to this isolated HOME."""
    try:
        manifest, files = import_encrypted(archive, _sync_password(password_stdin))
        environment_mapping: dict[str, Path] = {}
        if manifest.project_ids:
            if target_project and target_project_root:
                raise ValidationError("use either --target-project or --target-project-root")
            if target_project:
                environment_mapping = _target_mapping_by_id(manifest.project_ids, target_project)
            elif target_project_root:
                if len(manifest.project_ids) != 1 or len(target_project_root) != 1:
                    raise ValidationError(
                        "multiple projects require --target-project PROJECT_ID=TARGET"
                    )
                environment_mapping = {
                    manifest.project_ids[0]: _project_directory(target_project_root[0], local=True)
                }
            else:
                environment_mapping = {
                    project_id: _project_directory(
                        click.prompt(
                            f"Target project directory for agent environment {project_id}"
                        ),
                        local=True,
                    )
                    for project_id in manifest.project_ids
                }
        if "config" in manifest.selection and not apply_active_provider:
            raise ValidationError("archive config requires --apply-active-provider")
        if apply_active_provider and "config" not in manifest.selection:
            raise ValidationError("--apply-active-provider requires archive config selection")
        if apply_active_provider and not click.confirm(
            "Replace the target active Provider configuration?", default=False
        ):
            raise ValidationError("target active Provider replacement was not confirmed")
        source_slugs = sorted(
            {
                entry.path.split("/", 3)[2]
                for entry in manifest.files
                if entry.path.startswith("claude/projects/")
            }
        )
        claude_roots = {}
        if source_slugs and claude_project_root is not None:
            if claude_source_project is not None:
                source_slug = _claude_source_slug(files, source_slugs, claude_source_project)
            elif len(source_slugs) == 1:
                source_slug = source_slugs[0]
            else:
                raise ValidationError(
                    "multiple Claude projects require --claude-source-project or interactive mappings"
                )
            target_root = _project_directory(claude_project_root, local=True)
            manifest, files = select_claude_project(manifest, files, source_slug)
            claude_roots[source_slug] = target_root
        elif source_slugs:
            if claude_source_project is not None:
                raise ValidationError("--claude-source-project requires --claude-project-root")
            click.echo(
                "Claude Code project history must be mapped using real local project paths; "
                "storage keys are generated automatically."
            )
            for slug in source_slugs:
                source = _claude_source_project(files, slug)
                source_label = source or f"unknown path (storage key: {slug})"
                prompted_root = _prompt_project_directory(
                    f"Local project directory for Claude source project {source_label}",
                    local=True,
                )
                if prompted_root is not None:
                    claude_roots[slug] = prompted_root
            manifest, files = select_claude_projects(manifest, files, tuple(claude_roots))
        if claude_roots:
            manifest, files = map_claude_sessions(manifest, files, claude_roots)
        codex_roots = (
            _codex_project_mapping(files, local=True)
            if any(entry.path.startswith("codex/") for entry in manifest.files)
            else {}
        )
        if any(entry.path.startswith("codex/") for entry in manifest.files):
            manifest, files = select_codex_projects(manifest, files, tuple(codex_roots))
        _sync_process_preflight(manifest.selection, _manifest_clients(manifest))
        missing = apply_import(
            _home(),
            manifest,
            files,
            overwrite,
            None,
            codex_project_roots=codex_roots,
            environment_project_roots=environment_mapping,
        )
        payload = {"transfer_id": manifest.transfer_id, "files": sorted(files), "missing": missing}
        click.echo(json.dumps(payload, indent=2))
    except CodeluxError as exc:
        raise click.ClickException(str(exc)) from exc


@sync_group.command(name="push")
@click.option("--ssh", "ssh_target", help="SSH target, for example root@example.com.")
@click.option("--sessions", "include_sessions", is_flag=True)
@click.option("--claude-sessions", is_flag=True, help="Synchronize Claude Code project history.")
@click.option("--codex-sessions", is_flag=True, help="Synchronize Codex session history.")
@click.option("--providers", "include_providers", is_flag=True)
@click.option("--project-env", is_flag=True, help="Synchronize shared project agent files.")
@click.option("--local-project-env", is_flag=True, help="Include local project overrides.")
@click.option("--user-env", is_flag=True, help="Synchronize user-level agent configuration.")
@click.option("--memory", "include_memory", is_flag=True, help="Synchronize project memory.")
@click.option("--project-root", multiple=True, type=click.Path(path_type=Path))
@click.option("--target-project-root", multiple=True, type=click.Path(path_type=Path))
@click.option(
    "--project-map",
    multiple=True,
    help="Explicit environment mapping as SOURCE=TARGET; required for multiple projects.",
)
@click.option(
    "--keys/--no-keys",
    default=True,
    help="Compatibility option; official account files are never synchronized.",
)
@click.option("--overwrite", is_flag=True)
@click.option(
    "--claude-project-root",
    type=click.Path(path_type=Path),
    help="Target project root for Claude session history.",
)
@click.option(
    "--claude-source-project",
    help="Real absolute source Claude project path to map when syncing one project.",
)
@click.option(
    "--no-incremental",
    is_flag=True,
    help="Use the complete stream; does not bypass conflict checks.",
)
@_sync_locked
def sync_push(
    ssh_target: Optional[str],
    include_sessions: bool,
    claude_sessions: bool,
    codex_sessions: bool,
    include_providers: bool,
    project_env: bool,
    local_project_env: bool,
    user_env: bool,
    include_memory: bool,
    project_root: tuple[Path, ...],
    target_project_root: tuple[Path, ...],
    project_map: tuple[str, ...],
    keys: bool,
    overwrite: bool,
    claude_project_root: Optional[Path],
    claude_source_project: Optional[str],
    no_incremental: bool,
) -> None:
    """Interactively transfer selected state from this machine to B."""
    del no_incremental
    try:
        selected, session_clients, overwrite_clients = _push_selection(
            include_sessions,
            include_providers,
            claude_sessions,
            codex_sessions,
            overwrite,
            project_env,
            local_project_env,
            user_env,
            include_memory,
        )
        if not selected:
            click.echo("No content selected; synchronization cancelled.")
            return
        _sync_process_preflight(selected, session_clients)
        click.echo("[1/6] Checking source Codelux state...")
        registry_backup = _standardize_sync_source(selected)
        _ensure_sync_source_healthy(selected, session_clients)
        target = ssh_target or str(click.prompt("SSH target (user@host)"))
        if claude_project_root is not None:
            claude_project_root = _project_directory(claude_project_root, local=False)
        environment_mapping: dict[str, Path] = {}
        environment_selected = bool(
            set(selected).intersection({"project_env", "local_env", "memory"})
        )
        if project_map and (project_root or target_project_root):
            raise ValidationError("use --project-map instead of separate project-root options")
        if environment_selected and project_map:
            environment_source_roots, environment_mapping = _explicit_project_mappings(
                project_map, source_local=True, target_local=False
            )
        elif environment_selected:
            environment_source_roots = _environment_project_roots(
                project_root, local=True, prompt="Source project directory", default_cwd=True
            )
            if len(environment_source_roots) > 1 or len(target_project_root) > 1:
                raise ValidationError("multiple projects require --project-map SOURCE=TARGET")
        else:
            environment_source_roots = ()
        click.echo("[2/6] Collecting source files...")
        manifest, files = build_manifest(
            _home(),
            selected,
            keys,
            clients=session_clients,
            project_roots=environment_source_roots,
        )
        manifest, payload = materialize_sync_files(manifest, files)
        if environment_source_roots and not environment_mapping:
            environment_targets = _environment_targets_for_sources(
                environment_source_roots, target_project_root, local=False
            )
            environment_mapping = _environment_target_mapping(
                manifest, environment_source_roots, environment_targets
            )
        elif environment_mapping and set(environment_mapping) != set(manifest.project_ids):
            raise ValidationError("project environment source mapping does not match the archive")
        if "sessions" in selected:
            project_roots: dict[str, Path] = {}
            codex_roots: dict[str, Path] = {}
            source_slugs = sorted(
                {
                    entry.path.split("/", 3)[2]
                    for entry in manifest.files
                    if entry.path.startswith("claude/projects/")
                }
            )
            if source_slugs:
                if claude_project_root is not None:
                    selected_source = claude_source_project
                    if len(source_slugs) > 1 and not selected_source:
                        click.echo("Available Claude projects:")
                        for index, slug in enumerate(source_slugs, 1):
                            source = _claude_source_project(payload, slug)
                            click.echo(f"  {index}. {source or 'source path unavailable'}")
                        click.echo("Select one project by entering its number or real source path.")
                        selection = str(click.prompt("Selection (for example, 1)"))
                        if selection.isdigit() and 1 <= int(selection) <= len(source_slugs):
                            selected_source = source_slugs[int(selection) - 1]
                        else:
                            selected_source = _claude_source_slug(payload, source_slugs, selection)
                    elif selected_source:
                        selected_source = _claude_source_slug(
                            payload, source_slugs, selected_source
                        )
                    else:
                        selected_source = source_slugs[0]
                    if selected_source not in source_slugs:
                        raise ValidationError("unknown Claude source project")
                    manifest, payload = select_claude_project(
                        manifest,
                        payload,
                        selected_source,
                    )
                    project_roots[selected_source] = claude_project_root.absolute()
                    manifest, payload = map_claude_sessions(manifest, payload, project_roots)
                else:
                    click.echo(
                        "Claude Code project history must be mapped using real target project paths; "
                        "storage keys are generated automatically."
                    )
                    selected_slugs = []
                    for slug in source_slugs:
                        source = _claude_source_project(payload, slug)
                        source_label = source or f"unknown path (storage key: {slug})"
                        target_root = _prompt_project_directory(
                            f"Target project directory for Claude source project {source_label}",
                            local=False,
                        )
                        if target_root is None:
                            continue
                        selected_slugs.append(slug)
                        project_roots[slug] = target_root
                    manifest, payload = select_claude_projects(manifest, payload, selected_slugs)
                    manifest, payload = map_claude_sessions(manifest, payload, project_roots)
            if "codex" in session_clients:
                codex_roots = _codex_project_mapping(payload, local=False)
                manifest, payload = select_codex_projects(manifest, payload, tuple(codex_roots))
                manifest, payload = map_codex_sessions(
                    manifest, payload, _home().absolute(), codex_roots
                )
            overwrite_clients = _session_overwrite_prompts(session_clients, overwrite)
        click.echo(
            f"[3/6] Preparing transfer {manifest.transfer_id} ({len(manifest.files)} files)..."
        )
        archive = create_plain_archive(manifest, tuple(payload.items()))
        click.echo("[4/6] Connecting to the target and checking capabilities...")
        transport_kwargs: dict[str, Any] = {}
        if environment_mapping:
            transport_kwargs["environment_project_roots"] = environment_mapping
        if overwrite_clients:
            capability, response = push_archive(
                target,
                manifest,
                archive,
                overwrite,
                progress=lambda message: click.echo(f"[4/6] {message}"),
                overwrite_clients=overwrite_clients,
                **transport_kwargs,
            )
        else:
            capability, response = push_archive(
                target,
                manifest,
                archive,
                overwrite,
                progress=lambda message: click.echo(f"[4/6] {message}"),
                **transport_kwargs,
            )
        click.echo(
            f"[5/6] Target accepted protocol 1 ({capability.codelux_version}); transaction committed."
        )
        click.echo("[6/6] Verifying synchronization result...")
        click.echo(json.dumps(response, ensure_ascii=True, indent=2))
    except CodeluxError as exc:
        if "registry_backup" in locals() and registry_backup is not None:
            atomic_write_private(
                _home() / ".codelux/providers.json", registry_backup, _home() / ".codelux"
            )
        raise click.ClickException(str(exc)) from exc


@sync_group.command(name="pull")
@click.option("--ssh", "ssh_target", help="SSH source, for example root@example.com.")
@click.option("--sessions", "include_sessions", is_flag=True)
@click.option("--claude-sessions", is_flag=True, help="Synchronize Claude Code project history.")
@click.option("--codex-sessions", is_flag=True, help="Synchronize Codex session history.")
@click.option("--providers", "include_providers", is_flag=True)
@click.option("--project-env", is_flag=True, help="Synchronize shared project agent files.")
@click.option("--local-project-env", is_flag=True, help="Include local project overrides.")
@click.option("--user-env", is_flag=True, help="Synchronize user-level agent configuration.")
@click.option("--memory", "include_memory", is_flag=True, help="Synchronize project memory.")
@click.option("--project-root", multiple=True, type=click.Path(path_type=Path))
@click.option("--target-project-root", multiple=True, type=click.Path(path_type=Path))
@click.option(
    "--project-map",
    multiple=True,
    help="Explicit environment mapping as SOURCE=TARGET; required for multiple projects.",
)
@click.option(
    "--keys/--no-keys",
    default=True,
    help="Compatibility option; official account files are never synchronized.",
)
@click.option("--overwrite", is_flag=True)
@click.option(
    "--claude-project-root",
    type=click.Path(path_type=Path),
    help="Local project root for one Claude session-history project.",
)
@click.option(
    "--no-incremental",
    is_flag=True,
    help="Use the complete stream; does not bypass conflict checks.",
)
@_sync_locked
def sync_pull(
    ssh_target: Optional[str],
    include_sessions: bool,
    claude_sessions: bool,
    codex_sessions: bool,
    include_providers: bool,
    project_env: bool,
    local_project_env: bool,
    user_env: bool,
    include_memory: bool,
    project_root: tuple[Path, ...],
    target_project_root: tuple[Path, ...],
    project_map: tuple[str, ...],
    keys: bool,
    overwrite: bool,
    claude_project_root: Optional[Path],
    no_incremental: bool,
) -> None:
    """Pull selected state from B and apply it through the local sync transaction."""
    del no_incremental
    try:
        selected, session_clients, overwrite_clients = _push_selection(
            include_sessions,
            include_providers,
            claude_sessions,
            codex_sessions,
            overwrite,
            project_env,
            local_project_env,
            user_env,
            include_memory,
        )
        if not selected:
            click.echo("No content selected; synchronization cancelled.")
            return
        overwrite_clients = _session_overwrite_prompts(session_clients, overwrite)
        _sync_process_preflight(selected, session_clients)
        target = ssh_target or str(click.prompt("SSH source (user@host)"))
        environment_mapping: dict[str, Path] = {}
        environment_selected = bool(
            set(selected).intersection({"project_env", "local_env", "memory"})
        )
        if project_map and (project_root or target_project_root):
            raise ValidationError("use --project-map instead of separate project-root options")
        if environment_selected and project_map:
            requested_environment_roots, environment_mapping = _explicit_project_mappings(
                project_map, source_local=False, target_local=True
            )
        elif environment_selected:
            requested_environment_roots = _environment_project_roots(
                project_root, local=False, prompt="Source project directory on remote machine"
            )
            if len(requested_environment_roots) > 1 or len(target_project_root) > 1:
                raise ValidationError("multiple projects require --project-map SOURCE=TARGET")
        else:
            requested_environment_roots = ()
        click.echo("[1/4] Checking local Codelux state...")
        capability, manifest, files = pull_archive(
            target,
            _home(),
            selected,
            keys,
            progress=lambda message: click.echo(f"[2/4] {message}"),
            clients=session_clients,
            project_roots=requested_environment_roots,
        )
        if manifest.project_ids and not environment_mapping:
            environment_targets = _environment_targets_for_sources(
                requested_environment_roots, target_project_root, local=True
            )
            environment_mapping = _environment_target_mapping(
                manifest, requested_environment_roots, environment_targets
            )
        elif environment_mapping and set(environment_mapping) != set(manifest.project_ids):
            raise ValidationError("project environment source mapping does not match the archive")
        if "claude" in session_clients:
            source_slugs = sorted(
                {
                    entry.path.split("/", 3)[2]
                    for entry in manifest.files
                    if entry.path.startswith("claude/projects/")
                }
            )
            claude_roots = {}
            if claude_project_root is not None:
                if len(source_slugs) != 1:
                    raise ValidationError(
                        "multiple Claude projects require interactive target mappings"
                    )
                claude_roots[source_slugs[0]] = _project_directory(claude_project_root, local=True)
            else:
                click.echo(
                    "Claude Code project history must be mapped using real local project paths; "
                    "storage keys are generated automatically."
                )
                for slug in source_slugs:
                    source = _claude_source_project(files, slug)
                    source_label = source or f"unknown path (storage key: {slug})"
                    target_root = _prompt_project_directory(
                        f"Local project directory for Claude source project {source_label}",
                        local=True,
                    )
                    if target_root is not None:
                        claude_roots[slug] = target_root
            manifest, files = select_claude_projects(manifest, files, tuple(claude_roots))
            manifest, files = map_claude_sessions(manifest, files, claude_roots)
        codex_roots = (
            _codex_project_mapping(files, local=True) if "codex" in session_clients else {}
        )
        if "codex" in session_clients:
            manifest, files = select_codex_projects(manifest, files, tuple(codex_roots))
        click.echo(
            f"[3/4] Source accepted protocol 1 ({capability.codelux_version}); applying locally..."
        )
        missing = apply_import(
            _home(),
            manifest,
            files,
            overwrite
            if overwrite
            else {"claude": "claude" in overwrite_clients, "codex": "codex" in overwrite_clients},
            None,
            codex_project_roots=codex_roots,
            environment_project_roots=environment_mapping,
            operation_type="sync_pull",
        )
        click.echo("[4/4] Local transaction committed.")
        click.echo(
            json.dumps(
                {"status": "committed", "transfer_id": manifest.transfer_id, "missing": missing},
                ensure_ascii=True,
                indent=2,
            )
        )
    except CodeluxError as exc:
        raise click.ClickException(str(exc)) from exc


def _manifest_clients(manifest: object) -> tuple[str, ...]:
    files = getattr(manifest, "files", ())
    return tuple(
        name
        for name in ("claude", "codex")
        if any(getattr(entry, "path", "").startswith(f"{name}/") for entry in files)
    )


def _ensure_sync_source_healthy(
    selection: tuple[str, ...], clients: Optional[tuple[str, ...]] = None
) -> None:
    adapters, registry, _ = _adapters()
    names = [
        name for name in ("claude", "codex") if name in adapters and adapters[name].is_installed()
    ]
    if clients is not None:
        names = [name for name in names if name in clients]
    if "config" not in selection and "sessions" not in selection:
        return
    for name in names:
        observed = adapters[name].inspect()
        if observed.state in {ConfigState.UNKNOWN, ConfigState.EXTERNAL_OVERRIDE}:
            raise ValidationError(
                f"{name} is not in a recognized Codelux state; adopt it with add or repair it before sync"
            )
        expected = registry.current.get(name)
        actual = observed.provider_id if observed.state is ConfigState.CUSTOM else None
        if expected != actual:
            raise ValidationError(
                f"{name} is configured but Registry is drifted; switch explicitly before sync"
            )


def _standardize_sync_source(
    selection: tuple[str, ...], clients: tuple[str, ...] = ()
) -> Optional[bytes]:
    """Adopt an unregistered, clearly identified custom binding before push."""
    if "config" not in selection and "providers" not in selection:
        return None
    adapters, registry, root = _adapters()
    additions: dict[str, ProviderRecord] = {}
    for name, adapter in adapters.items():
        if clients and name not in clients:
            continue
        observed = adapter.inspect()
        if observed.state in {
            ConfigState.EXTERNAL_OVERRIDE,
            ConfigState.OFFICIAL_LOGIN,
            ConfigState.OFFICIAL_API_KEY,
        }:
            continue
        if observed.provider_id is None and observed.base_url is None:
            continue
        provider_name = observed.provider_id or "custom"
        if not observed.base_url:
            raise ValidationError(f"{name} custom Provider URL cannot be determined")
        key: Optional[str] = None
        if name == "codex":
            try:
                auth = json.loads((adapter.home / ".codex/auth.json").read_bytes())
                key = auth.get("OPENAI_API_KEY") if auth.get("auth_mode") == "apikey" else None
            except (OSError, json.JSONDecodeError):
                key = None
        else:
            try:
                settings = json.loads((adapter.home / ".claude/settings.json").read_bytes())
                key = settings.get("env", {}).get("ANTHROPIC_AUTH_TOKEN")
            except (OSError, json.JSONDecodeError):
                key = None
        if not isinstance(key, str) or not key:
            raise ValidationError(f"{name} custom Provider key cannot be determined")
        old = additions.get(provider_name) or registry.providers.get(provider_name)
        bindings = dict(old.clients) if old is not None else {}
        bindings[name] = ClientBinding(observed.base_url, key, "responses", True)
        additions[provider_name] = ProviderRecord(
            provider_name, bindings, old.description if old else ""
        )
    if not additions:
        return None
    backup = (root / "providers.json").read_bytes() if (root / "providers.json").is_file() else None
    providers = dict(registry.providers)
    providers.update(additions)
    current = dict(registry.current)
    for provider_name, provider in additions.items():
        for client in provider.clients:
            current[client] = provider_name
    save_registry(root, Registry(registry.schema_version, providers, current))
    for name in additions:
        click.echo(f"[1/6] Registered detected custom Provider {name} for synchronization")
    return backup


def _sync_process_preflight(
    selection: tuple[str, ...], clients: Optional[tuple[str, ...]] = None
) -> None:
    if not set(selection).intersection(
        {"config", "sessions", "project_env", "local_env", "user_env", "memory"}
    ):
        return
    if set(selection).intersection({"project_env", "local_env", "user_env", "memory"}):
        clients = None
    adapters, _, _ = _adapters()
    home = _home()
    installed = {
        "claude": (home / ".claude").exists(),
        "codex": (home / ".codex").exists(),
    }
    unsafe = [
        name
        for name, adapter in adapters.items()
        if installed[name]
        and (clients is None or name in clients)
        and adapter.is_running() is not ProcessState.NOT_RUNNING
    ]
    if unsafe:
        raise ValidationError(
            "sync clients are running or process state is unknown: " + ", ".join(unsafe)
        )


def _machine_id_for_home(home: Path) -> Optional[str]:
    path = home / ".codelux" / "machine-id"
    return path.read_text().strip() if path.is_file() and not path.is_symlink() else None


@sync_group.command(name="reset")
@click.option("--machine", "remote_machine", required=True)
@click.option(
    "--selection",
    multiple=True,
    type=click.Choice(
        ["config", "sessions", "providers", "project_env", "local_env", "user_env", "memory"]
    ),
)
@_sync_locked
def sync_reset(remote_machine: str, selection: tuple[str, ...]) -> None:
    """Forget sync baselines for a remote machine."""
    removed = reset_baseline(_home() / ".codelux", remote_machine, selection or None)
    click.echo("baseline reset" if removed else "no matching baseline")


@sync_group.command(name="status")
def sync_status() -> None:
    """Show local synchronization identity and baseline metadata."""
    try:
        root = _home() / ".codelux"
        state = load_sync_state(root)
        identity = (
            (root / "machine-id").read_text().strip() if (root / "machine-id").is_file() else None
        )
        click.echo(json.dumps({"machine_id": identity, "baselines": state["baselines"]}, indent=2))
    except CodeluxError as exc:
        raise click.ClickException(str(exc)) from exc


@sync_group.group(name="transport")
def sync_transport_group() -> None:
    """Internal fixed-command SSH transport endpoints."""


@sync_transport_group.command(name="receive")
@click.option("--protocol", type=int, required=True)
@click.option("--overwrite", is_flag=True)
@click.option("--overwrite-claude", is_flag=True)
@click.option("--overwrite-codex", is_flag=True)
@click.option("--claude-project-root", type=click.Path(path_type=Path))
@click.option("--project-map-stdin", is_flag=True)
@_sync_locked
def sync_transport_receive(
    protocol: int,
    overwrite: bool,
    overwrite_claude: bool,
    overwrite_codex: bool,
    claude_project_root: Optional[Path],
    project_map_stdin: bool,
) -> None:
    """Receive one validated plaintext archive over the authenticated SSH stream."""
    try:
        if protocol != 1:
            raise ValidationError("unsupported sync protocol")
        capability = local_capability(_home())
        click.echo(json.dumps(capability.to_dict(), sort_keys=True), nl=True)
        input_stream = sys.stdin.buffer
        environment_mapping = {}
        if project_map_stdin:
            decoded = read_path_payload(input_stream)
            if not isinstance(decoded, dict):
                raise ValidationError("project path payload is invalid")
            environment_mapping = {
                str(project_id): _project_directory(target, local=True)
                for project_id, target in decoded.items()
            }
        raw = input_stream.read()
        manifest, files = parse_plain_archive(raw)
        validate_capability(capability, manifest)
        _sync_process_preflight(manifest.selection, _manifest_clients(manifest))
        for project_path in _session_project_directories(files):
            _project_directory(project_path, local=True)
        overwrite_policy = overwrite or {
            "claude": overwrite_claude,
            "codex": overwrite_codex,
            "providers": overwrite,
        }
        missing = apply_import(
            _home(),
            manifest,
            files,
            overwrite_policy,
            claude_project_root,
            environment_project_roots=environment_mapping,
            operation_type="sync_push",
        )
        click.echo(
            json.dumps(
                {"status": "committed", "transfer_id": manifest.transfer_id, "missing": missing}
            )
        )
    except CodeluxError as exc:
        raise click.ClickException(str(exc)) from exc


@sync_transport_group.command(name="send")
@click.option("--protocol", type=int, required=True)
@click.option("--config", "include_config", is_flag=True)
@click.option("--sessions", "include_sessions", is_flag=True)
@click.option("--providers", "include_providers", is_flag=True)
@click.option("--project-env", is_flag=True)
@click.option("--local-project-env", is_flag=True)
@click.option("--user-env", is_flag=True)
@click.option("--memory", "include_memory", is_flag=True)
@click.option("--project-roots-stdin", is_flag=True)
@click.option("--claude-sessions", is_flag=True)
@click.option("--codex-sessions", is_flag=True)
@click.option("--keys/--no-keys", default=True)
@_sync_locked
def sync_transport_send(
    protocol: int,
    include_config: bool,
    include_sessions: bool,
    include_providers: bool,
    project_env: bool,
    local_project_env: bool,
    user_env: bool,
    include_memory: bool,
    project_roots_stdin: bool,
    claude_sessions: bool,
    codex_sessions: bool,
    keys: bool,
) -> None:
    """Send one validated plaintext archive over the authenticated SSH stream."""
    try:
        if protocol != 1:
            raise ValidationError("unsupported sync protocol")
        selected = _sync_selection(
            include_config,
            include_sessions,
            include_providers,
            project_env,
            local_project_env,
            user_env,
            include_memory,
        )
        project_roots: tuple[Path, ...] = ()
        if project_roots_stdin:
            decoded = read_path_payload(sys.stdin.buffer)
            if not isinstance(decoded, list) or any(not isinstance(item, str) for item in decoded):
                raise ValidationError("project path payload is invalid")
            project_roots = tuple(_project_directory(item, local=True) for item in decoded)
        clients = tuple(
            client
            for client, enabled in (("claude", claude_sessions), ("codex", codex_sessions))
            if enabled
        )
        if include_sessions and not clients:
            clients = ("claude", "codex")
        _sync_process_preflight(selected, clients)
        _ensure_sync_source_healthy(selected, clients)
        manifest, files = build_manifest(
            _home(),
            selected,
            keys,
            clients=clients or None,
            project_roots=project_roots,
        )
        manifest, payload = materialize_sync_files(manifest, files)
        archive = create_plain_archive(manifest, tuple(payload.items()))
        output = sys.stdout.buffer
        output.write(canonical_line(local_capability(_home()).to_dict()))
        output.write(archive)
        output.flush()
    except CodeluxError as exc:
        raise click.ClickException(str(exc)) from exc


@sync_group.group(name="machine-id")
def sync_machine_id() -> None:
    """Manage the local synchronization identity."""


@sync_machine_id.command(name="rotate")
@_sync_locked
def sync_machine_id_rotate() -> None:
    """Rotate identity and clear all local baselines."""
    value = rotate_machine_id(_home() / ".codelux")
    click.echo(f"rotated machine-id {value}")


def _read_secret(label: str, client: str, key_stdin: bool) -> str:
    env_name = f"CODELUX_{client.upper()}_API_KEY"
    if key_stdin:
        value = sys.stdin.read().strip()
    elif env_name in os.environ:
        value = os.environ[env_name]
    else:
        value = str(click.prompt(label, hide_input=True, confirmation_prompt=True, type=str))
    if not value:
        raise ValidationError("credential must not be empty")
    return value


def _locked(command: F) -> F:
    @wraps(command)
    def wrapper(*args: object, **kwargs: object) -> None:
        root = _home() / ".codelux"
        with OperationLock(root / "operation.lock"):
            recovery = root / "recovery.json"
            if command.__name__ != "recover" and (recovery.is_file() or recovery.is_symlink()):
                raise click.ClickException("recovery is required before another write operation")
            if command.__name__ != "recover":
                try:
                    load_registry(root)
                except CodeluxError as exc:
                    raise click.ClickException(str(exc)) from exc
                incomplete = SnapshotStore(root).latest_incomplete()
                if incomplete is not None:
                    SnapshotStore(root).require_recovery(incomplete)
                    raise click.ClickException(
                        f"incomplete operation {incomplete.operation_id} requires recovery"
                    )
            command(*args, **kwargs)

    return wrapper  # type: ignore[return-value]


def _save_registry(root: Path, registry: Registry) -> None:
    save_registry(root, registry)


def _home() -> Path:
    configured = os.environ.get("CODELUX_TEST_HOME")
    return Path(configured).expanduser().absolute() if configured else Path.home()


def _adapters() -> tuple:
    home = _home()
    root = home / ".codelux"
    registry = load_registry(root)
    return (
        {"claude": ClaudeAdapter(home, registry), "codex": CodexAdapter(home, registry)},
        registry,
        root,
    )


def _resolve_client(requested: str, adapters: dict) -> list:
    installed = [name for name, adapter in adapters.items() if adapter.is_installed()]
    if requested:
        if requested not in adapters or not adapters[requested].is_installed():
            raise click.ClickException(f"client is not installed: {requested}")
        return [requested]
    if len(installed) != 1:
        raise click.ClickException("multiple or no clients detected; specify --client")
    return installed


def _resolve_status_clients(requested: str, adapters: dict) -> list:
    if requested:
        return _resolve_client(requested, adapters)
    installed = [name for name, adapter in adapters.items() if adapter.is_installed()]
    if not installed:
        raise click.ClickException("no supported clients are installed")
    return installed


def _repair_advice(state: ConfigState, health: HealthState) -> str:
    if health is HealthState.RECOVERY_REQUIRED:
        return "run codelux recover --dry-run, then codelux recover"
    if state is ConfigState.EXTERNAL_OVERRIDE:
        return "unset the conflicting client environment variable and inspect again"
    if health is HealthState.DRIFTED:
        return "inspect the client configuration, then use add to adopt it or switch explicitly"
    if state is ConfigState.UNKNOWN:
        return "repair the client configuration or restore a verified official snapshot"
    return ""


@main.command()
@click.option("--client", type=click.Choice(["claude", "codex"]))
@click.option("--format", "output_format", type=click.Choice(["table", "json"]))
def status(client: str, output_format: str) -> None:
    """Inspect actual client configuration."""
    try:
        adapters, registry, root = _adapters()
        if output_format is None:
            output_format = "table"
        names = _resolve_status_clients(client, adapters)
        incomplete = SnapshotStore(root).latest_incomplete()
        recovery_required = (root / "recovery.json").is_file() or incomplete is not None
        rows = []
        for name in names:
            observed = adapters[name].inspect()
            process = adapters[name].is_running()
            expected = registry.desired.get(name)
            observed_provider = (
                observed.provider_id if observed.state is ConfigState.CUSTOM else None
            )
            health = HealthState.HEALTHY
            if recovery_required:
                health = HealthState.RECOVERY_REQUIRED
            elif observed.state is ConfigState.UNKNOWN or expected != observed_provider:
                health = HealthState.DRIFTED
            rows.append(
                {
                    "client": name,
                    "state": observed.state.value,
                    "health": health.value,
                    "process": process.value,
                    "provider_id": observed.provider_id,
                    "base_url": observed.base_url,
                    "fingerprint": observed.fingerprint,
                    "reasons": list(observed.reasons)
                    + (
                        [f"incomplete operation: {incomplete.operation_id}"]
                        if incomplete is not None
                        else []
                    ),
                    "repair": _repair_advice(observed.state, health),
                }
            )
        if output_format == "json":
            click.echo(json.dumps(rows, ensure_ascii=True, indent=2))
        else:
            for row in rows:
                provider = (
                    f"provider={row['provider_id']}, " if row["provider_id"] is not None else ""
                )
                click.echo(
                    f"{row['client']}: {row['state']} "
                    f"({provider}{row['health']}, process={row['process']})"
                )
                if row["repair"]:
                    click.echo(f"  repair: {row['repair']}")
                if row["reasons"]:
                    click.echo("  reasons: " + "; ".join(row["reasons"]))
    except CodeluxError as exc:
        raise click.ClickException(str(exc)) from exc


@main.command(name="add")
@click.argument("name")
@click.option("--url", help="Provider base URL; prompted when omitted.")
@click.option("--client", type=click.Choice(["claude", "codex"]))
@click.option("--key-stdin", is_flag=True, help="Read one key from stdin (single client only).")
@_locked
def add_provider(name: str, url: Optional[str], client: str, key_stdin: bool) -> None:
    """Register and activate one Provider binding."""
    try:
        validate_provider_name(name)
        adapters, registry, root = _adapters()
        names = _resolve_client(client, adapters)
        target = names[0]
        existing_provider = registry.providers.get(name)
        if existing_provider is not None and target in existing_provider.clients:
            raise ValidationError(f"Provider already has a {target} binding: {name}")
        if adapters[target].is_running() is not ProcessState.NOT_RUNNING:
            raise ValidationError(f"{target} is running or process state is unknown")
        observed = adapters[target].inspect()
        fresh_codex_install = (
            target == "codex"
            and not adapters[target].config_path.exists()
            and not adapters[target].auth_path.exists()
        )
        if (
            observed.state in {ConfigState.UNKNOWN, ConfigState.EXTERNAL_OVERRIDE}
            and not fresh_codex_install
        ):
            raise ValidationError(f"{target} configuration is {observed.state.value}")
        if url is None:
            url = str(click.prompt(f"{target} base URL", type=str))
        key = _read_secret(f"{target} API key", target, key_stdin)
        binding = (
            ClientBinding(url, key, "responses", True)
            if target == "codex"
            else ClientBinding(url, key)
        )
        for existing_name, existing in registry.providers.items():
            if existing.clients.get(target) == binding:
                raise ValidationError(
                    f"the {target} binding is already registered as Provider {existing_name}"
                )
        providers = dict(registry.providers)
        bindings = dict(existing_provider.clients) if existing_provider is not None else {}
        bindings[target] = binding
        providers[name] = ProviderRecord(
            name,
            bindings,
            existing_provider.description if existing_provider is not None else "",
        )
        candidate_registry = type(registry)(registry.schema_version, providers, registry.current)
        candidate_adapter = (
            ClaudeAdapter(_home(), candidate_registry)
            if target == "claude"
            else CodexAdapter(_home(), candidate_registry)
        )
        adopted = candidate_adapter.inspect()
        if adopted.state is ConfigState.CUSTOM and adopted.provider_id == name:
            current = dict(registry.desired)
            current[target] = name
            _save_registry(root, type(registry)(registry.schema_version, providers, current))
            click.echo(f"added {name} and adopted the current {target} configuration")
            return

        payload = binding.to_dict()
        payload["provider_id"] = name
        change = adapters[target].prepare_provider(payload)
        store = SnapshotStore(root)
        manifest = store.create((change,), "add", name, {target: registry.desired.get(target)})
        TransactionCoordinator(adapters).commit_all((change,), manifest, store)
        current = dict(registry.desired)
        current[target] = name
        _save_registry_after_switch(
            root,
            type(registry)(registry.schema_version, providers, current),
            adapters,
            (change,),
            manifest,
            store,
        )
        click.echo(f"added and activated {name} for {target}")
    except CodeluxError as exc:
        raise click.ClickException(str(exc)) from exc


@main.command(name="list")
@click.option("--format", "output_format", type=click.Choice(["table", "json"]))
def list_providers(output_format: str) -> None:
    """List every selectable Provider without exposing credentials."""
    try:
        _, registry, _ = _adapters()
        if output_format is None:
            output_format = "table"
        rows: list[ProviderListRow] = [
            {
                "name": "official",
                "clients": sorted(SUPPORTED_CLIENTS),
                "description": "Built-in official Provider",
                "builtin": True,
            }
        ]
        for provider in registry.providers.values():
            rows.append(
                {
                    "name": provider.name,
                    "clients": sorted(provider.clients),
                    "description": provider.description,
                    "builtin": False,
                }
            )
        if output_format == "json":
            click.echo(json.dumps(rows, ensure_ascii=True, indent=2))
        else:
            for row in rows:
                marker = " [builtin]" if row["builtin"] else ""
                click.echo(f"{row['name']}{marker}: {', '.join(row['clients'])}")
    except CodeluxError as exc:
        raise click.ClickException(str(exc)) from exc


@main.command(name="update")
@click.argument("name")
@click.option("--client", type=click.Choice(["claude", "codex"]), required=True)
@click.option("--url", help="Replace the Provider base URL together with the credential.")
@click.option("--key-stdin", is_flag=True, help="Read one key from stdin (single client only).")
@_locked
def update_provider(name: str, client: str, url: Optional[str], key_stdin: bool) -> None:
    """Replace the URL and credential for an existing Provider binding."""
    try:
        adapters, registry, root = _adapters()
        names = _resolve_client(client, adapters)
        target = names[0]
        provider = registry.providers.get(name)
        if provider is None:
            raise ValidationError(f"unknown Provider: {name}")
        old = provider.clients.get(target)
        if old is None:
            raise ValidationError(f"Provider has no {target} binding")
        observed = adapters[target].inspect()
        if observed.state in {ConfigState.UNKNOWN, ConfigState.EXTERNAL_OVERRIDE}:
            raise ValidationError(f"cannot update while {target} state is uncertain")
        observed_provider = observed.provider_id if observed.state is ConfigState.CUSTOM else None
        if registry.desired.get(target) != observed_provider:
            raise ValidationError(f"cannot update while {target} Registry state is drifted")
        active = registry.desired.get(target) == name and observed_provider == name
        process = adapters[target].is_running() if active else ProcessState.NOT_RUNNING
        if process is ProcessState.UNKNOWN:
            raise ValidationError(f"{target} process state is unknown")
        if url is None:
            url = str(click.prompt(f"{target} base URL", default=old.base_url, type=str))
        key = _read_secret(f"{target} API key", target, key_stdin)
        bindings = dict(provider.clients)
        bindings[target] = ClientBinding(
            url,
            key,
            old.wire_api,
            old.requires_openai_auth,
            old.enabled,
        )
        providers = dict(registry.providers)
        providers[name] = ProviderRecord(name, bindings, provider.description)
        updated_registry = type(registry)(registry.schema_version, providers, registry.current)
        if active:
            payload = bindings[target].to_dict()
            payload["provider_id"] = name
            change = (
                adapters[target].prepare_provider(payload, migrate_sessions=False)
                if target == "codex"
                else adapters[target].prepare_provider(payload)
            )
            store = SnapshotStore(root)
            manifest = store.create(
                (change,), "update", name, {target: registry.desired.get(target)}
            )
            TransactionCoordinator(adapters).commit_all((change,), manifest, store)
            _save_registry_after_switch(
                root,
                updated_registry,
                adapters,
                (change,),
                manifest,
                store,
            )
            click.echo(f"updated active Provider {name}; restart {target} to apply the change")
        else:
            _save_registry(root, updated_registry)
            click.echo(f"updated {name}")
    except CodeluxError as exc:
        raise click.ClickException(str(exc)) from exc


@main.command(name="remove")
@click.argument("name")
@click.option("--client", type=click.Choice(["claude", "codex"]))
@click.option("--force", is_flag=True)
@_locked
def remove_provider(name: str, client: str, force: bool) -> None:
    """Remove a Provider binding after checking actual client usage."""
    try:
        adapters, registry, root = _adapters()
        provider = registry.providers.get(name)
        if provider is None:
            raise ValidationError(f"unknown Provider: {name}")
        names = _resolve_client(client, adapters) if client else list(provider.clients)
        for target in names:
            if target not in provider.clients:
                continue
            observed = adapters[target].inspect()
            if observed.state in {ConfigState.UNKNOWN, ConfigState.EXTERNAL_OVERRIDE}:
                raise ValidationError(f"cannot remove while {target} state is uncertain")
            observed_provider = (
                observed.provider_id if observed.state is ConfigState.CUSTOM else None
            )
            if observed.state is ConfigState.CUSTOM and observed_provider is None:
                raise ValidationError(f"cannot remove while {target} Provider is unrecognized")
            if registry.desired.get(target) != observed_provider:
                raise ValidationError(f"cannot remove while {target} Registry state is drifted")
            if observed.provider_id == name:
                raise ValidationError(f"Provider {name} is active for {target}")
        if not force and _provider_has_history(SnapshotStore(root), name, names):
            raise ValidationError(
                f"Provider {name} has historical snapshots; use --force to remove it"
            )
        providers = dict(registry.providers)
        if set(names) >= set(provider.clients):
            providers.pop(name)
        else:
            remaining = {key: value for key, value in provider.clients.items() if key not in names}
            providers[name] = ProviderRecord(name, remaining, provider.description)
        current = dict(registry.desired)
        for target in names:
            if current.get(target) == name:
                current[target] = None
        _save_registry(root, type(registry)(registry.schema_version, providers, current))
        click.echo(f"removed {name}")
    except CodeluxError as exc:
        raise click.ClickException(str(exc)) from exc


@main.command()
@click.argument("name")
@click.option("--client", type=click.Choice(["claude", "codex"]), required=True)
@click.option(
    "--no-shared-session",
    is_flag=True,
    help="Keep same-agent Codex sessions separated by Provider.",
)
@_locked
def switch(name: str, client: str, no_shared_session: bool) -> None:
    """Switch a client to a registered Provider."""
    try:
        adapters, registry, root = _adapters()
        names = _resolve_client(client, adapters)
        if name == "official":
            store = SnapshotStore(root)
            changes = []
            native_login_targets = []
            session_sources = set(registry.providers) | {"openai", "custom"}
            for target in names:
                if adapters[target].is_running() is not ProcessState.NOT_RUNNING:
                    raise ValidationError(f"{target} is running or process state is unknown")
                observed = adapters[target].inspect()
                if observed.state in {
                    ConfigState.OFFICIAL_LOGIN,
                    ConfigState.OFFICIAL_API_KEY,
                }:
                    click.echo(f"{target} is already using the official configuration")
                    continue
                if (
                    target == "codex"
                    and isinstance(adapters[target], CodexAdapter)
                    and adapters[target].has_native_official_login()
                ):
                    changes.append(
                        adapters[target].prepare_native_official_restore(
                            shared_session=not no_shared_session,
                            session_sources=session_sources,
                        )
                    )
                    continue
                try:
                    manifest = _latest_official_manifest(store, target)
                except ValidationError:
                    if (
                        target == "claude"
                        and isinstance(adapters[target], ClaudeAdapter)
                        and observed.state is ConfigState.CUSTOM
                        and observed.provider_id in registry.providers
                    ):
                        changes.append(adapters[target].prepare_native_official_login())
                        native_login_targets.append(target)
                        continue
                    raise
                if target == "codex":
                    changes.append(
                        adapters[target].prepare_snapshot_restore(
                            manifest.to_dict(),
                            shared_session=not no_shared_session,
                            session_sources=session_sources,
                        )
                    )
                else:
                    changes.append(adapters[target].prepare_snapshot_restore(manifest.to_dict()))
            if not changes:
                return
            restore_manifest = store.create(
                tuple(changes),
                "switch",
                "official",
                {target: registry.current.get(target) for target in names},
            )
            TransactionCoordinator(adapters).commit_all(tuple(changes), restore_manifest, store)
            current = dict(registry.current)
            for target in names:
                current[target] = None
            _save_registry_after_switch(
                root,
                type(registry)(registry.schema_version, registry.providers, current),
                adapters,
                tuple(changes),
                restore_manifest,
                store,
            )
            if native_login_targets:
                click.echo(
                    "prepared Claude for official login; sign in with your official account:\n"
                    "  claude"
                )
            else:
                click.echo(f"restored official state for {', '.join(names)}")
            return
        provider = registry.providers.get(name)
        if provider is None:
            raise ValidationError(f"unknown Provider: {name}")
        changes = []
        session_sources = set(registry.providers) | {"openai"}
        for target in names:
            binding = provider.clients.get(target)
            if binding is None or not binding.enabled:
                raise ValidationError(f"Provider has no enabled {target} binding")
            if adapters[target].is_running() is not ProcessState.NOT_RUNNING:
                raise ValidationError(f"{target} is running or process state is unknown")
            observed = adapters[target].inspect()
            # An explicit switch is also the repair path for a parseable client
            # whose active Provider is no longer present in the Registry.
            # The adapter still rejects malformed or unsafe files while
            # preparing the change. External process overrides remain blocked.
            if observed.state is ConfigState.EXTERNAL_OVERRIDE:
                raise ValidationError(f"{target} configuration is {observed.state.value}")
            payload = binding.to_dict()
            payload["provider_id"] = name
            if target == "codex":
                changes.append(
                    adapters[target].prepare_provider(
                        payload,
                        shared_session=not no_shared_session,
                        session_sources=session_sources,
                    )
                )
            else:
                changes.append(adapters[target].prepare_provider(payload))
        store = SnapshotStore(root)
        manifest = store.create(
            tuple(changes),
            "switch",
            name,
            {target: registry.current.get(target) for target in names},
        )
        TransactionCoordinator(adapters).commit_all(tuple(changes), manifest, store)
        updated_current = dict(registry.current)
        for target in names:
            updated_current[target] = name
        _save_registry_after_switch(
            root,
            type(registry)(registry.schema_version, registry.providers, updated_current),
            adapters,
            tuple(changes),
            manifest,
            store,
        )
        click.echo(f"switched {', '.join(names)} to {name}")
    except CodeluxError as exc:
        raise click.ClickException(str(exc)) from exc


@main.command()
@click.option("--operation", "operation_id")
@click.option("--dry-run", is_flag=True)
@_locked
def recover(operation_id: str, dry_run: bool) -> None:
    """Validate and restore files recorded by recovery.json."""
    try:
        _, _, root = _adapters()
        store = SnapshotStore(root)
        recovery_path = root / "recovery.json"
        if not recovery_path.is_file() and not recovery_path.is_symlink():
            incomplete = store.latest_incomplete()
            if incomplete is not None:
                store.require_recovery(incomplete)
        if not recovery_path.is_file() or recovery_path.is_symlink():
            raise ValidationError("no recovery is required")
        payload = json.loads(recovery_path.read_bytes())
        selected = operation_id or payload.get("operation_id")
        if not isinstance(selected, str) or selected != payload.get("operation_id"):
            raise ValidationError("recovery operation does not match recovery.json")
        manifest = store.read_manifest(selected)
        if dry_run:
            click.echo(json.dumps(manifest.to_dict(), ensure_ascii=True, indent=2))
            return
        if any(
            adapter.is_running() is not ProcessState.NOT_RUNNING
            for adapter in _adapters()[0].values()
        ):
            raise ValidationError("a client is running or process state is unknown")
        store.recover(_home(), selected)
        click.echo(f"recovered operation {selected}")
    except (CodeluxError, json.JSONDecodeError) as exc:
        raise click.ClickException(str(exc)) from exc


def _latest_official_manifest(store: SnapshotStore, client: str) -> Manifest:
    candidates = []
    if not store.backups.exists():
        raise ValidationError(_official_snapshot_error(client, exists=False))
    for operation_dir in store.backups.iterdir():
        if not operation_dir.is_dir() or operation_dir.is_symlink():
            continue
        try:
            manifest = store.read_manifest(operation_dir.name)
        except ValidationError:
            continue
        state = manifest.before_states.get(client)
        if state in {ConfigState.OFFICIAL_LOGIN, ConfigState.OFFICIAL_API_KEY} or (
            client == "codex"
            and state is ConfigState.UNKNOWN
            and _manifest_has_codex_chatgpt_login(store, manifest)
        ):
            candidates.append(manifest)
    if not candidates:
        raise ValidationError(_official_snapshot_error(client, exists=True))
    return sorted(candidates, key=lambda item: item.created_at)[-1]


def _manifest_has_codex_chatgpt_login(store: SnapshotStore, manifest: Manifest) -> bool:
    auth_file = next(
        (item for item in manifest.files if item.source_path == "codex/auth.json"),
        None,
    )
    if auth_file is None:
        return False
    try:
        auth = json.loads((store.root / auth_file.backup_path).read_bytes())
    except (OSError, json.JSONDecodeError):
        return False
    return (
        isinstance(auth, dict)
        and auth.get("auth_mode") == "chatgpt"
        and isinstance(auth.get("tokens"), dict)
        and any(auth["tokens"].values())
    )


def _official_snapshot_error(client: str, *, exists: bool) -> str:
    reason = (
        f"no official snapshot exists for {client}"
        if not exists
        else f"no verified official snapshot exists for {client}"
    )
    if client == "codex":
        product = "Codex"
        login_command = "codex login --device-auth"
        return f"{reason}.\n  Sign in to {product} with your official account:\n    {login_command}"
    return (
        f"{reason}.\n"
        "  Claude is still using third-party routing; running claude cannot start official login.\n"
        "  Register or adopt the current Provider, then rerun:\n"
        "    codelux switch official --client claude"
    )


def _provider_has_history(store: SnapshotStore, provider_name: str, clients: list) -> bool:
    if not store.backups.is_dir() or store.backups.is_symlink():
        return False
    selected = set(clients)
    for operation_dir in store.backups.iterdir():
        if not operation_dir.is_dir() or operation_dir.is_symlink():
            continue
        try:
            manifest = store.read_manifest(operation_dir.name)
        except ValidationError:
            continue
        if manifest.target_provider == provider_name and selected.intersection(manifest.clients):
            return True
    return False


def _save_registry_after_switch(
    root: Path,
    registry: Registry,
    adapters: dict,
    changes: tuple,
    manifest: Manifest,
    store: SnapshotStore,
) -> None:
    try:
        _save_registry(root, registry)
    except Exception as exc:
        rollback_errors = []
        manifest_state = manifest
        for change in reversed(changes):
            try:
                adapters[change.client].rollback(change)
                manifest_state = store.update_client_state(
                    manifest_state,
                    change.client,
                    FileState.ROLLED_BACK,
                    OperationState.ROLLING_BACK,
                )
            except Exception as rollback_exc:
                rollback_errors.append(rollback_exc)
                manifest_state = store.update_client_state(
                    manifest_state,
                    change.client,
                    FileState.RECOVERY_REQUIRED,
                    OperationState.RECOVERY_REQUIRED,
                )
        if rollback_errors:
            store.write_recovery(manifest_state)
            raise RecoveryRequiredError(
                "registry write failed and client recovery is required"
            ) from rollback_errors[0]
        store.set_operation_state(manifest_state, OperationState.ROLLED_BACK)
        raise CodeluxError("registry write failed; client changes were rolled back") from exc


if __name__ == "__main__":
    main()
