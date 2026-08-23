from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ALLOWED_ROOT_FILES = {
    ".gitignore",
    "LICENSE",
    "README.md",
    "README.zh-CN.md",
    "SECURITY.md",
    "SECURITY.zh-CN.md",
    "pyproject.toml",
}
ALLOWED_ROOT_DIRS = {".github", "scripts", "src", "tests"}
ALLOWED_WORKFLOWS = {"build.yml", "ci.yml", "publish.yml", "publish-pypi.yml"}
ALLOWED_SCRIPTS = {"check_public_repository.py", "release_artifacts.py"}
ALLOWED_COMMIT_IDENTITIES = {
    ("codelux-ai-dev", "316519841+codelux-ai-dev@users.noreply.github.com"),
    ("GitHub", "noreply@github.com"),
}

CONTENT_RULES = {
    "email address": re.compile(
        r"\b[A-Za-z0-9._%+-]+@"
        r"(?!example[.](?:com|net|org)\b)"
        r"(?![A-Za-z0-9.-]+[.]example\b)"
        r"[A-Za-z0-9.-]+[.][A-Za-z]{2,}\b",
        re.IGNORECASE,
    ),
    "local user path": re.compile(
        r"(?:~/Documents/|/Users/(?!test/|source/|runner/|example/))", re.IGNORECASE
    ),
    "privileged local path": re.compile(r"/root/"),
    "non-public project subdomain": re.compile(
        r"\b(?:[A-Za-z0-9-]+[.])+codelux[.]io\b", re.IGNORECASE
    ),
    "real root login": re.compile(r"root@(?!example[.]com)", re.IGNORECASE),
    "IPv4 address": re.compile(r"(?<![0-9])(?:[0-9]{1,3}[.]){3}[0-9]{1,3}(?![0-9])"),
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    "GitHub token": re.compile(r"(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})"),
    "long API key": re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
}


def git_lines(*args: str) -> list[str]:
    result = subprocess.run(["git", *args], cwd=ROOT, check=True, capture_output=True, text=True)
    return [line for line in result.stdout.splitlines() if line]


def candidate_files() -> list[str]:
    return sorted(set(git_lines("ls-files", "--cached", "--others", "--exclude-standard")))


def check_file_boundary(files: list[str]) -> list[str]:
    errors: list[str] = []
    for name in files:
        path = Path(name)
        root = path.parts[0]
        if len(path.parts) == 1:
            if name not in ALLOWED_ROOT_FILES:
                errors.append(f"unapproved root file: {name}")
            continue
        if root not in ALLOWED_ROOT_DIRS:
            errors.append(f"unapproved root directory: {root}")
        elif root == ".github" and (
            len(path.parts) != 3
            or path.parts[1] != "workflows"
            or path.parts[2] not in ALLOWED_WORKFLOWS
        ):
            errors.append(f"unapproved GitHub file: {name}")
        elif root == "scripts" and (len(path.parts) != 2 or path.parts[1] not in ALLOWED_SCRIPTS):
            errors.append(f"unapproved script: {name}")
    return errors


def check_bilingual_docs(files: list[str]) -> list[str]:
    errors: list[str] = []
    markdown = {name for name in files if name.endswith(".md")}
    for name in sorted(markdown):
        if name.endswith(".zh-CN.md"):
            counterpart = name[: -len(".zh-CN.md")] + ".md"
        else:
            counterpart = name[: -len(".md")] + ".zh-CN.md"
        if counterpart not in markdown:
            errors.append(f"missing bilingual counterpart: {name} -> {counterpart}")
    return errors


def check_content(files: list[str]) -> list[str]:
    errors: list[str] = []
    for name in files:
        if name == "scripts/check_public_repository.py":
            continue
        data = (ROOT / name).read_bytes()
        if b"\0" in data:
            continue
        text = data.decode("utf-8")
        for label, pattern in CONTENT_RULES.items():
            match = pattern.search(text)
            if match:
                errors.append(f"{name}: forbidden {label}: {match.group(0)!r}")
    return errors


def check_commit_identities() -> list[str]:
    try:
        records = git_lines("log", "--format=%an%x09%ae%x09%cn%x09%ce")
    except subprocess.CalledProcessError:
        return []
    errors: list[str] = []
    for record in records:
        author_name, author_email, committer_name, committer_email = record.split("\t")
        for kind, identity in (
            ("author", (author_name, author_email)),
            ("committer", (committer_name, committer_email)),
        ):
            if identity not in ALLOWED_COMMIT_IDENTITIES:
                errors.append(f"unapproved commit {kind}: {identity[0]} <{identity[1]}>")
    return errors


def check_release_language() -> list[str]:
    publish = (ROOT / ".github/workflows/publish.yml").read_text()
    required = ("## English", "## 简体中文")
    return [
        f"publish workflow is missing bilingual release marker: {item}"
        for item in required
        if item not in publish
    ]


def main() -> None:
    files = candidate_files()
    errors = [
        *check_file_boundary(files),
        *check_bilingual_docs(files),
        *check_content(files),
        *check_commit_identities(),
        *check_release_language(),
    ]
    if errors:
        raise SystemExit("public repository policy failed:\n- " + "\n- ".join(errors))
    print(f"public repository policy passed ({len(files)} files checked)")


if __name__ == "__main__":
    main()
