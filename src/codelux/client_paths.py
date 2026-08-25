"""Resolve supported client storage roots without copying machine-managed policy."""

import os
from pathlib import Path

from codelux.errors import ValidationError


def claude_config_root(home: Path) -> Path:
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    if not configured:
        return home / ".claude"
    if configured == "~":
        return home
    if configured.startswith("~/"):
        return home / configured[2:]
    return Path(configured).expanduser().absolute()


def claude_project_slug(project_root: Path) -> str:
    resolved = project_root.expanduser().absolute()
    return "-" + "-".join(part for part in resolved.parts if part not in ("/", ""))


def claude_project_memory_root(home: Path, project_root: Path) -> Path:
    project_directory = os.environ.get("CLAUDE_CODE_PROJECT_DIR_NAME") or claude_project_slug(
        project_root
    )
    if not project_directory or Path(project_directory).name != project_directory:
        raise ValidationError("CLAUDE_CODE_PROJECT_DIR_NAME is invalid")
    return claude_config_root(home) / "projects" / project_directory / "memory"
