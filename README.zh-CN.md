# Codelux

[English](README.md) | [简体中文](README.zh-CN.md)

Codelux 是一个面向 [Claude Code](https://www.anthropic.com/claude-code) 和 [OpenAI Codex](https://github.com/openai/codex) 的命令行 Provider 管理工具。它与 [cc-switch](https://github.com/farion1231/cc-switch) 解决同一类问题，但采用更简洁、终端优先的实现：Provider 只需注册一次，即可检查健康状态并明确切换，无需手工编辑 JSON 或 TOML 配置文件。

**官网与 Provider API：** [https://codelux.io](https://codelux.io)

## Codelux 与 cc-switch

[cc-switch](https://github.com/farion1231/cc-switch) 是面向多种 AI 客户端的跨平台桌面管理工具。Codelux 则专注于喜欢轻量命令行工具的用户：

- **终端优先：** 适合 Shell 工作流、远程服务器和无桌面环境。
- **聚焦核心场景：** 当前管理 Claude Code 和 Codex，而不是覆盖大量桌面客户端。
- **变更明确：** `add`、`switch`、`update`、`remove` 都是需要明确指定客户端的操作。
- **保守安全：** 修改前先检查配置状态；未知、冲突或不完整状态默认拒绝修改。
- **本地变更可恢复：** 切换前创建快照，并使用私有文件写入保护原配置。
- **兼容公开 API：** 可使用任何兼容 Claude 或 OpenAI/Codex API 的 Provider，包括 [codelux.io](https://codelux.io)。

Codelux 并不试图覆盖 cc-switch 的全部功能：需要广泛图形化管理时可以选择 cc-switch；需要紧凑、可审计的命令行工具时，可以选择 Codelux。

## 主要能力

- 管理 Claude Code 和 Codex 的 Provider
- 只读检查配置与进程健康状态
- 在官方配置和自定义 Provider 之间明确切换
- 通过快照恢复官方配置
- 对未知或不完整本地状态执行失败关闭
- 加密离线归档和基于 OpenSSH 的同步
- 对同步配置和会话数据执行明确的冲突处理
- 按需同步 Claude Code 和 Codex 的项目/用户环境及项目记忆

## 安全模型

Codelux 将 Provider 凭据保存在受操作系统文件权限保护的本地文件中。这些文件未进行静态加密，无法防御以同一用户身份运行的进程、特权管理员、恶意软件、账号失陷、备份泄露或物理磁盘访问。

跨机器归档使用用户提供的密码加密。除非你明确要求，否则不会在另一台机器上应用活动客户端配置。

漏洞报告方式和详细安全边界见 [SECURITY.zh-CN.md](SECURITY.zh-CN.md)。

## 安装

从 PyPI 安装最新正式发布版本：

```bash
python3 -m pip install --upgrade codelux
```

查看已安装版本：

```bash
codelux --version
```

请先通过官方渠道单独安装 Claude Code 和 Codex，再使用 Codelux 管理它们。

## 使用方法

查看全部命令：

```bash
codelux --help
```

查看 Codelux 版本：

```bash
codelux version
```

检查 Claude Code 当前配置和进程状态：

```bash
codelux status --client claude
```

列出已注册的 Provider，不显示凭据：

```bash
codelux list
```

注册 Provider 并为 Claude Code 激活。输入 API key 时不会回显：

```bash
codelux add codelux-io --url https://codelux.io --client claude
```

为 Codex 激活已经注册的 Provider：

```bash
codelux switch codelux-io --client codex
```

将 Claude Code 恢复到官方配置或官方登录流程：

```bash
codelux switch official --client claude
```

替换已有 Provider 绑定的 URL 或凭据：

```bash
codelux update codelux-io --client claude
```

在 Codelux 检查 Provider 是否仍被使用后，删除绑定：

```bash
codelux remove codelux-io --client claude
```

通过 SSH 将选定的 Provider 状态同步到另一台机器：

```bash
codelux sync push --ssh user@host.example --providers
```

通过 SSH 从另一台机器同步选定的 Provider 状态：

```bash
codelux sync pull --ssh user@host.example --providers
```

同步 Claude Code 或 Codex 会话历史时，请输入目标机器上的真实绝对项目目录。在 macOS 和
Linux 上，最可靠的方式是在目标项目中运行 `pwd` 并粘贴输出。不要输入
`-Users-user-work-project` 这类 Claude Code 内部存储键；Codelux 会自动生成该键。本地 pull
目标和远端 push 目标都必须已经存在且是目录。未选择同步会话的客户端不需要停止。

同步项目的可移植 Agent 环境、本地覆盖、用户级 Agent 配置和 Claude 项目记忆：

```bash
codelux sync push --ssh user@host.example \
  --project-env --local-project-env --user-env --memory \
  --project-map /work/my-project=/srv/my-project
```

共享项目白名单包括分层 `AGENTS.md`、`AGENTS.override.md`、`CLAUDE.md`、项目内 Claude
导入文件、`.mcp.json`、选定的 Claude settings/rules/skills/agents/commands，以及选定的
Codex 配置/rules/hooks。`CLAUDE.local.md`、`.claude/settings.local.json` 等仅本地文件必须
显式使用 `--local-project-env`。同步多个项目时，重复提供 `--project-map SOURCE=TARGET`；
映射是显式的，不依赖参数顺序。离线导入只暴露不透明项目 ID，并使用
`--target-project PROJECT_ID=TARGET` 指定目标。

认证数据库、OAuth/账号状态、Provider 路由、Codex trust 和用户级 Codex MCP server 表
不会同步。JSON 中疑似秘密的字段会被移除；如果 MCP command 参数数组包含凭据参数名或
已识别的令牌前缀，整个参数数组都会被清空，需要在目标机器重新配置该命令。自由格式的
指令和命令仍可能包含私密内容，因此传输前应审查所选文件，并使用加密导出或可信 SSH 对端。

可以对任意命令使用 `--help` 查看当前选项和安全提示。

## 当前支持的系统环境

- macOS 12 或更高版本，支持 Intel 和 Apple Silicon
- 安装 Python 3.9–3.12 的 Linux 发行版
- Claude Code 和 Codex 已安装，并且命令位于 `PATH`
- 可写的用户主目录，用于客户端配置和 Codelux 状态文件

当前暂不支持 Windows 作为运行环境。Codelux 仍处于 Alpha 阶段，正式使用前请在目标环境验证具体的 Claude Code/Codex 版本和 Provider API 兼容性。

## 许可证

MIT
