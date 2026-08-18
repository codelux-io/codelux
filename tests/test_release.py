import importlib.util
import json
from pathlib import Path
import sys

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised by the Python 3.9/3.10 CI matrix
    import tomli as tomllib

import pytest

from codelux import __version__


ROOT = Path(__file__).parents[1]
SCRIPT_PATH = ROOT / "scripts/release_artifacts.py"
PYPI_WORKFLOW_PATH = ROOT / ".github/workflows/publish-pypi.yml"
SPEC = importlib.util.spec_from_file_location("release_artifacts", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
release_artifacts = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = release_artifacts
SPEC.loader.exec_module(release_artifacts)

POLICY_PATH = ROOT / "scripts/check_public_repository.py"
POLICY_SPEC = importlib.util.spec_from_file_location("check_public_repository", POLICY_PATH)
assert POLICY_SPEC is not None and POLICY_SPEC.loader is not None
public_policy = importlib.util.module_from_spec(POLICY_SPEC)
sys.modules[POLICY_SPEC.name] = public_policy
POLICY_SPEC.loader.exec_module(public_policy)


def test_package_versions_match() -> None:
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert metadata["tool"]["poetry"]["version"] == __version__


def test_pypi_workflow_is_production_only() -> None:
    workflow = PYPI_WORKFLOW_PATH.read_text()
    assert "environment: production" in workflow
    assert "https://upload.pypi.org/legacy/" in workflow
    assert "test.pypi.org" not in workflow
    assert "gh release" not in workflow
    assert "contents: read" in workflow
    assert "contents: write" not in workflow


@pytest.mark.parametrize(
    ("rule", "sample"),
    [
        ("email address", "operator" + "@" + "personal.invalid"),
        ("local user path", "/" + "Users/example-person/project"),
        ("privileged local path", "/" + "root/example-project"),
        ("non-public project subdomain", "internal.example." + "codelux.io"),
        ("real root login", "root" + "@host.invalid"),
        ("IPv4 address", "192.0.2." + "10"),
    ],
)
def test_public_policy_uses_generic_sensitive_content_rules(rule: str, sample: str) -> None:
    assert public_policy.CONTENT_RULES[rule].search(sample)


@pytest.mark.parametrize(
    "sample",
    [
        "root@example.com",
        "user@host.example",
        "/Users/test/project",
        "/Users/source/project",
        "https://codelux.io",
    ],
)
def test_public_policy_allows_reserved_examples_and_public_endpoint(sample: str) -> None:
    assert not any(pattern.search(sample) for pattern in public_policy.CONTENT_RULES.values())


def test_release_manifest_round_trip_and_tamper_detection(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    metadata = tmp_path / "metadata"
    dist.mkdir()
    (dist / "codelux-0.1.0a1-py3-none-any.whl").write_bytes(b"wheel")
    (dist / "codelux-0.1.0a1.tar.gz").write_bytes(b"sdist")

    manifest = release_artifacts.generate_manifest(
        dist,
        metadata,
        version="0.1.0a1",
        head_sha="a" * 40,
        build_run_id="123",
        repository="codelux-io/codelux",
        workflow="build.yml",
    )
    release_artifacts.verify_manifest(
        manifest,
        dist,
        expected_version="0.1.0a1",
        expected_head_sha="a" * 40,
        expected_build_run_id="123",
        expected_repository="codelux-io/codelux",
        expected_workflow="build.yml",
    )

    (dist / "codelux-0.1.0a1.tar.gz").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="hash mismatch"):
        release_artifacts.verify_manifest(
            manifest,
            dist,
            expected_version="0.1.0a1",
            expected_head_sha="a" * 40,
            expected_build_run_id="123",
            expected_repository="codelux-io/codelux",
            expected_workflow="build.yml",
        )


def test_release_manifest_rejects_binding_mismatch(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    metadata = tmp_path / "metadata"
    dist.mkdir()
    (dist / "codelux-0.1.0a1-py3-none-any.whl").write_bytes(b"wheel")
    (dist / "codelux-0.1.0a1.tar.gz").write_bytes(b"sdist")
    manifest = release_artifacts.generate_manifest(
        dist,
        metadata,
        version="0.1.0a1",
        head_sha="a" * 40,
        build_run_id="123",
        repository="codelux-io/codelux",
        workflow="build.yml",
    )
    raw = json.loads(manifest.read_text())
    raw["repository"] = "attacker/fork"
    manifest.write_text(json.dumps(raw))

    with pytest.raises(ValueError, match="repository mismatch"):
        release_artifacts.verify_manifest(
            manifest,
            dist,
            expected_version="0.1.0a1",
            expected_head_sha="a" * 40,
            expected_build_run_id="123",
            expected_repository="codelux-io/codelux",
            expected_workflow="build.yml",
        )


def test_remote_artifact_comparison_allows_only_matching_subsets(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    wheel = dist / "codelux-0.1.0a1-py3-none-any.whl"
    sdist = dist / "codelux-0.1.0a1.tar.gz"
    wheel.write_bytes(b"wheel")
    sdist.write_bytes(b"sdist")
    wheel_hash = release_artifacts._sha256(wheel)
    sdist_hash = release_artifacts._sha256(sdist)

    assert release_artifacts.compare_remote_artifacts(
        dist, {wheel.name: wheel_hash}, allow_missing=True
    ) == [sdist]
    assert (
        release_artifacts.compare_remote_artifacts(
            dist,
            {wheel.name: wheel_hash, sdist.name: sdist_hash},
            allow_missing=False,
        )
        == []
    )

    with pytest.raises(ValueError, match="unexpected files"):
        release_artifacts.compare_remote_artifacts(
            dist,
            {wheel.name: wheel_hash, sdist.name: sdist_hash, "extra.whl": "0" * 64},
            allow_missing=False,
        )
    with pytest.raises(ValueError, match="hash mismatch"):
        release_artifacts.compare_remote_artifacts(dist, {wheel.name: "0" * 64}, allow_missing=True)
    with pytest.raises(ValueError, match="incomplete"):
        release_artifacts.compare_remote_artifacts(
            dist, {wheel.name: wheel_hash}, allow_missing=False
        )
