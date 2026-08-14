# Codelux

[English](README.md) | [简体中文](README.zh-CN.md)

Codelux is a command-line tool for managing LLM Provider configurations across AI coding
assistants. It provides explicit Provider switching, recoverable local changes, and encrypted
cross-machine synchronization.

**Website and Provider API:** [https://codelux.io](https://codelux.io)

## Capabilities

- Provider management for Claude Code and Codex
- Read-only configuration and health inspection
- Snapshot-backed restoration of official configurations
- Fail-closed recovery for incomplete local transactions
- Encrypted offline export/import and OpenSSH-based synchronization
- Explicit conflict handling for synchronized configuration and session data

## Security model

Codelux stores Provider credentials in local files protected by operating-system file permissions.
These files are not encrypted at rest. They are not protected from processes running as the same
user, privileged administrators, malware, compromised accounts, backups, or physical disk access.

Cross-machine archives are encrypted with a user-provided password. Active client configuration is
not applied on another machine unless the user explicitly requests it.

See [SECURITY.md](SECURITY.md) for vulnerability reporting and security boundaries.

## Installation

Codelux is currently in alpha and has not been published to the production Python Package Index.
Install the development checkout with:

```bash
python3 -m pip install -e .
```

## Usage

```bash
codelux version
codelux status --client claude
codelux list
codelux add codelux-io --url https://codelux.io --client claude
codelux switch codelux-io --client claude
codelux switch official --client claude
codelux recover --dry-run
```

Synchronization commands support explicit content selection:

```bash
codelux sync export --output codelux-sync.enc --providers --sessions
codelux sync import codelux-sync.enc
codelux sync push --ssh user@host.example --providers
codelux sync pull --ssh user@host.example --providers
```

Use `--help` on any command to inspect its current options and safety prompts.

## Development

```bash
python3 -m pip install . pytest==7.4.4 coverage==7.10.7
coverage run -m pytest -q
coverage report
python scripts/check_public_repository.py
```

## License

MIT
