# Codelux

[English](README.md) | [简体中文](README.zh-CN.md)

Codelux is a focused command-line provider manager for [Claude Code](https://www.anthropic.com/claude-code) and [OpenAI Codex](https://github.com/openai/codex). It is a simpler, terminal-first alternative in the same problem space as [cc-switch](https://github.com/farion1231/cc-switch): register compatible Provider endpoints once, inspect their health, and switch clients explicitly without hand-editing JSON or TOML files.

**Website and Provider API:** [https://codelux.io](https://codelux.io)

## Codelux and cc-switch

[cc-switch](https://github.com/farion1231/cc-switch) is a broad, cross-platform desktop manager for many AI clients. Codelux takes a narrower approach for users who prefer a small command-line tool:

- **Terminal-first:** works directly in shell workflows and on headless machines.
- **Focused scope:** currently manages Claude Code and Codex rather than a large desktop client catalog.
- **Explicit changes:** `add`, `switch`, `update`, and `remove` are deliberate commands with clear client selection.
- **Conservative safety:** configuration states are inspected before changes; unknown, conflicting, or incomplete states fail closed.
- **Recoverable local changes:** snapshots and private file writes help preserve the previous configuration before switching.
- **Open API compatibility:** use any Provider that exposes a compatible Claude or OpenAI/Codex API, including [codelux.io](https://codelux.io).

Codelux is not a replacement for every cc-switch feature. Choose cc-switch for a broad graphical manager; choose Codelux when a compact, auditable CLI is the better fit.

## Features

- Provider management for Claude Code and Codex
- Read-only configuration and health inspection
- Explicit switching between official and custom Providers
- Snapshot-backed restoration of official configurations
- Fail-closed handling for unknown or incomplete local state
- Encrypted offline archives and OpenSSH-based synchronization
- Explicit conflict handling for synchronized configuration and session data

## Security model

Codelux stores Provider credentials in local files protected by operating-system file permissions. These files are not encrypted at rest and cannot protect against processes running as the same user, privileged administrators, malware, compromised accounts, backups, or physical disk access.

Cross-machine archives are encrypted with a user-provided password. Active client configuration is not applied on another machine unless you explicitly request it.

See [SECURITY.md](SECURITY.md) for vulnerability reporting and security boundaries.

## Installation

Install the latest published release from PyPI:

```bash
python3 -m pip install --upgrade codelux
```

Check the installed version:

```bash
codelux --version
```

Install Claude Code and Codex separately through their official distribution channels before managing them with Codelux.

## Usage

Show all available commands:

```bash
codelux --help
```

Show the installed Codelux version:

```bash
codelux version
```

Inspect the detected Claude Code configuration and process state:

```bash
codelux status --client claude
```

List registered Providers without displaying credentials:

```bash
codelux list
```

Register a Provider and activate it for Claude Code. Codelux prompts for the API key without echoing it:

```bash
codelux add codelux-io --url https://codelux.io --client claude
```

Activate an already registered Provider for Codex:

```bash
codelux switch codelux-io --client codex
```

Return Claude Code to its official configuration or login flow:

```bash
codelux switch official --client claude
```

Replace the URL or credential for an existing Provider binding:

```bash
codelux update codelux-io --client claude
```

Remove a Provider binding after Codelux checks whether it is still in use:

```bash
codelux remove codelux-io --client claude
```

Synchronize selected Provider state to another machine over SSH:

```bash
codelux sync push --ssh user@host.example --providers
```

Synchronize selected Provider state from another machine over SSH:

```bash
codelux sync pull --ssh user@host.example --providers
```

Use `--help` on any command to inspect its current options and safety prompts.

## Supported environments

- macOS 12 or later, Intel or Apple Silicon
- Linux distributions with Python 3.9–3.12
- Claude Code and Codex installed and available on `PATH`
- A writable user home directory for client configuration and Codelux state

Windows is not currently a supported runtime target. Codelux is alpha software; verify the exact Claude Code/Codex versions and Provider API compatibility in your environment before production use.

## License

MIT
