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
- Selective synchronization of Claude Code and Codex project/user environments and project memory

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

When synchronizing Claude Code or Codex session history, enter the real absolute project directory
on the target machine. The most reliable method on macOS and Linux is to run `pwd` inside that
project and paste its output. Do not enter Claude Code's internal storage key, such as
`-Users-user-work-project`; Codelux generates that key automatically. Local pull targets and remote
push targets must already exist and be directories. A client that is not selected for session
synchronization does not need to be stopped.

Synchronize a project's portable agent environment together with local overrides, user-level
agent configuration, and Claude project memory:

```bash
codelux sync push --ssh user@host.example \
  --project-env --local-project-env --user-env --memory \
  --project-map /work/my-project=/srv/my-project
```

Without content flags, `sync push` and `sync pull` present a guided checklist that describes each
scope and shows its default as `[Y/n]` or `[y/N]`; pressing Enter accepts the capitalized choice.
For project environment or memory, enter each source project root separately, then leave the next
source prompt empty to finish the list. For local-source operations such as `sync push`, Codelux
first discovers existing project roots referenced by Claude Code and Codex session history and asks
about each suggested project on its own `[y/N]` line. You can then add paths that were not suggested.
The supplemental prompt is `Additional source project directory (leave empty to finish)`, so an
empty response ends the list after at least one project has been selected.
For `sync pull`, Codelux first uses a separate read-only SSH command to discover candidates from the
remote session history, confirms them locally, and then sends the selected roots back when requesting
the archive. If the remote version does not support discovery, Codelux reports that suggestions are
unavailable and falls back to manual remote paths. Codelux asks for one target project root per source,
so the command may run from any directory and can synchronize multiple projects in one transfer. Run
`pwd` inside a project when you need its absolute path. The current directory is suggested only for
the first manual local source and only when it is outside the user home, preventing an accidental
whole-home project scan. Unix sockets, FIFOs, and device nodes found inside a valid project tree are
ignored because they are not portable files; symbolic links remain rejected.

The shared project allowlist includes hierarchical `AGENTS.md`, `AGENTS.override.md`, `CLAUDE.md`,
project-contained Claude imports, `.mcp.json`, selected Claude settings/rules/skills/agents/commands,
and selected Codex configuration/rules/hooks. Local-only files such as `CLAUDE.local.md` and
`.claude/settings.local.json` require `--local-project-env`. For noninteractive multi-project
synchronization, repeat `--project-map SOURCE=TARGET`; mappings are explicit and never depend on
option order. Offline imports expose only opaque project IDs and use
`--target-project PROJECT_ID=TARGET`.

Authentication databases, OAuth/account state, Provider routing, Codex trust, and user-level Codex
MCP server tables are excluded. Secret-shaped JSON fields are removed, and an MCP command argument
array is cleared in full when it contains a credential flag or recognized token prefix; reconfigure
that command on the target. Free-form instructions and commands can still contain private material,
so review selected files and use an encrypted export or a trusted SSH peer.

Use `--help` on any command to inspect its current options and safety prompts.

## AI collaborative development

Codelux is developed through human-guided collaboration between developers and AI coding agents.
Implementation, review, and governance are assigned by task: substantial changes are independently
reviewed where practical, claims are backed by reproducible tests and checks, and human maintainers
retain final responsibility for project direction, security boundaries, merges, and releases.

Developers and AI agents are welcome to join the project. Contributions can begin with an issue,
review, test, documentation improvement, or focused pull request. Please make the intended scope,
validation evidence, and any security or privacy assumptions clear so that both human and AI
collaborators can evaluate the change reliably.

## Supported environments

- macOS 12 or later, Intel or Apple Silicon
- Linux distributions with Python 3.9–3.12
- Claude Code and Codex installed and available on `PATH`
- A writable user home directory for client configuration and Codelux state

Windows is not currently a supported runtime target. Codelux is alpha software; verify the exact Claude Code/Codex versions and Provider API compatibility in your environment before production use.

## License

MIT
