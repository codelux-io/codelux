"""Persistence helpers for the local Provider registry."""

import json
from pathlib import Path

from codelux.errors import ValidationError
from codelux.registry import Registry
from codelux.safe_files import atomic_write_private, ensure_private_dir


def load_registry(root: Path) -> Registry:
    path = root / "providers.json"
    if not path.exists():
        return Registry()
    if path.is_symlink() or not path.is_file():
        raise ValidationError("Provider registry is unsafe")
    try:
        return Registry.from_dict(json.loads(path.read_bytes()))
    except (
        OSError,
        json.JSONDecodeError,
        ValidationError,
        TypeError,
        AttributeError,
        KeyError,
        ValueError,
    ) as exc:
        raise ValidationError("Provider registry is invalid") from exc


def save_registry(root: Path, registry: Registry) -> None:
    ensure_private_dir(root)
    payload = json.dumps(registry.to_dict(), ensure_ascii=True, indent=2).encode() + b"\n"
    atomic_write_private(root / "providers.json", payload, root)
