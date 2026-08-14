#!/usr/bin/env python3
"""Generate and verify immutable Codelux release artifact metadata."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Optional, Sequence


SCHEMA_VERSION = 1


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _distribution_files(dist: Path) -> list[Path]:
    files = sorted(path for path in dist.iterdir() if path.is_file())
    wheels = [path for path in files if path.suffix == ".whl"]
    sdists = [path for path in files if path.name.endswith(".tar.gz")]
    if len(files) != 2 or len(wheels) != 1 or len(sdists) != 1:
        raise ValueError("release dist must contain only wheel and sdist files")
    return files


def compare_remote_artifacts(
    dist: Path, remote_hashes: dict[str, str], *, allow_missing: bool
) -> list[Path]:
    """Verify remote artifacts and return files that still need uploading."""
    files = _distribution_files(dist)
    local_hashes = {path.name: _sha256(path) for path in files}
    unexpected = set(remote_hashes) - set(local_hashes)
    if unexpected:
        raise ValueError("remote release artifact set contains unexpected files")
    missing = []
    for path in files:
        remote_hash = remote_hashes.get(path.name)
        if remote_hash is None:
            missing.append(path)
        elif remote_hash != local_hashes[path.name]:
            raise ValueError(f"remote release artifact hash mismatch: {path.name}")
    if missing and not allow_missing:
        raise ValueError("remote release artifact set is incomplete")
    return missing


def generate_manifest(
    dist: Path,
    metadata: Path,
    *,
    version: str,
    head_sha: str,
    build_run_id: str,
    repository: str,
    workflow: str,
) -> Path:
    files = _distribution_files(dist)
    if len(head_sha) != 40 or any(character not in "0123456789abcdef" for character in head_sha):
        raise ValueError("head SHA must be a full lowercase Git commit SHA")
    metadata.mkdir(parents=True, exist_ok=True)
    artifacts = {path.name: _sha256(path) for path in files}
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "version": version,
        "head_sha": head_sha,
        "build_run_id": str(build_run_id),
        "repository": repository,
        "workflow": workflow,
        "built_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "artifacts": artifacts,
    }
    manifest_path = metadata / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    checksums = "".join(f"{digest}  {name}\n" for name, digest in artifacts.items())
    (metadata / "SHA256SUMS.txt").write_text(checksums)
    return manifest_path


def _load_manifest(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("release manifest is invalid") from exc
    required = {
        "schema_version",
        "version",
        "head_sha",
        "build_run_id",
        "repository",
        "workflow",
        "built_at",
        "artifacts",
    }
    if not isinstance(data, dict) or set(data) != required or data["schema_version"] != 1:
        raise ValueError("release manifest is invalid")
    if not isinstance(data["artifacts"], dict) or not data["artifacts"]:
        raise ValueError("release manifest is invalid")
    return data


def verify_manifest(
    manifest_path: Path,
    dist: Path,
    *,
    expected_version: str,
    expected_head_sha: str,
    expected_build_run_id: str,
    expected_repository: str,
    expected_workflow: str,
) -> None:
    data = _load_manifest(manifest_path)
    expectations = {
        "version": expected_version,
        "head_sha": expected_head_sha,
        "build_run_id": str(expected_build_run_id),
        "repository": expected_repository,
        "workflow": expected_workflow,
    }
    for field, expected in expectations.items():
        if data[field] != expected:
            raise ValueError(f"release manifest {field} mismatch")
    files = _distribution_files(dist)
    if set(data["artifacts"]) != {path.name for path in files}:
        raise ValueError("release manifest artifact set mismatch")
    for path in files:
        if data["artifacts"].get(path.name) != _sha256(path):
            raise ValueError(f"release artifact hash mismatch: {path.name}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("generate", "verify"):
        child = subparsers.add_parser(command)
        child.add_argument("--dist", type=Path, required=True)
        child.add_argument("--manifest", type=Path)
        child.add_argument("--metadata", type=Path)
        child.add_argument("--version", required=True)
        child.add_argument("--head-sha", required=True)
        child.add_argument("--build-run-id", required=True)
        child.add_argument("--repository", required=True)
        child.add_argument("--workflow", default="build.yml")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "generate":
        if args.metadata is None:
            raise SystemExit("--metadata is required for generate")
        generate_manifest(
            args.dist,
            args.metadata,
            version=args.version,
            head_sha=args.head_sha,
            build_run_id=args.build_run_id,
            repository=args.repository,
            workflow=args.workflow,
        )
    else:
        if args.manifest is None:
            raise SystemExit("--manifest is required for verify")
        verify_manifest(
            args.manifest,
            args.dist,
            expected_version=args.version,
            expected_head_sha=args.head_sha,
            expected_build_run_id=args.build_run_id,
            expected_repository=args.repository,
            expected_workflow=args.workflow,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
