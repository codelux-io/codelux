# Security Policy

[English](SECURITY.md) | [简体中文](SECURITY.zh-CN.md)

## Supported versions

Codelux is currently in alpha. Security fixes are provided for the latest published version only.

## Reporting a vulnerability

Use GitHub Private Vulnerability Reporting from the repository Security page. Do not report a
security vulnerability through a public issue, discussion, pull request, or commit.

Do not include API keys, access tokens, authentication files, personal information, local
filesystem paths, or real infrastructure addresses. Replace sensitive values with clearly fictional
examples before submitting evidence.

Include the following information when available:

- The affected Codelux version
- A concise description of the security impact
- Minimal reproduction steps
- Redacted logs or configuration
- A suggested mitigation, if known

Reports will be acknowledged after they are reviewed. Validation, remediation, release, and public
disclosure timing depend on severity and reproducibility. Please allow time for a fix before public
disclosure.

## Security boundary

Codelux stores Provider credentials in local files with restricted filesystem permissions. This is
not encryption and does not protect against processes running as the same user, privileged
administrators, malware, compromised accounts, backups, or physical disk access.

Codelux cannot guarantee the security of third-party Providers, AI clients, SSH servers, operating
systems, or networks. Users are responsible for protecting those systems and rotating credentials
when compromise is suspected.
