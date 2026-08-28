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
- 按需同步 Claude Code 和 Codex 的项目/用户环境及 Agent 记忆

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

普通 Provider 切换只更新路由和认证配置，不扫描或改写已有 Codex 会话历史。默认情况下，未来
创建的 Codex 会话使用共享的 `custom` Provider 别名，因此切换 Provider 后仍然可见。如需让未来
会话继续保留彼此分离的 Provider 标识，可使用 `--no-shared-session`。

如需整合分布在不同 Provider 下的同一 Codex Agent 历史会话，请先停止 Codex，再显式执行合并：

```bash
codelux sessions merge --client codex
```

该命令会把历史中所有非 `custom` Provider 标识改为共享的 `custom` 别名，包括已经删除或无法识别
的旧 Provider。处理时间可能随会话历史大小增长；当前 Provider、凭据和 Registry 均不会改变。
只有实际变化的历史文件会暂存在 `~/.codelux/sync-transactions` 以便回滚，合并成功后会删除临时
payload。

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

同步项目的可移植 Agent 环境、本地覆盖、用户级 Agent 配置、Codex 用户记忆和所选项目的
Claude 记忆：

```bash
codelux sync push --ssh user@host.example \
  --project-env --local-project-env --user-env --memory \
  --project-map /work/my-project=/srv/my-project
```

未提供内容范围参数时，`sync push` 和 `sync pull` 会显示引导式清单，逐项解释同步范围，并用
`[Y/n]` 或 `[y/N]` 标出默认选项；直接回车会接受其中的大写选项。同步项目环境或 Claude
项目记忆时，
请逐个输入每个源项目根目录，在下一个源目录提示处留空即可结束列表。Codelux 会为每个源项目
分别询问目标项目根目录。对于 `sync push` 这类源数据位于本机的操作，Codelux 会先从 Claude
Code 和 Codex 会话历史中发现仍然存在的项目根目录，在独立的 `[y/N]` 提示行中逐个询问是否
同步，然后允许继续补充未被建议的目录。补充输入行会明确显示
`Additional source project directory (leave empty to finish)`；已经选择至少一个项目后，直接
回车即可结束列表。`sync pull` 会先通过独立的只读 SSH 命令从远端会话历史发现候选目录，在
本机逐项确认后，再在请求正式归档时把所选根目录发回远端。如果远端版本尚不支持候选查询，
Codelux 会明确提示候选不可用，并回退到手工输入远端路径。命令可以从任意目录运行，也可以在
一次传输中同步多个项目。需要确认绝对路径时，可以进入相应项目执行 `pwd`。只有第一个手工
本地源项目、且当前目录不属于用户主目录时，Codelux 才会建议当前目录作为快捷默认值，避免
误扫整个主目录。有效项目树中的 Unix socket、FIFO 和设备节点不属于可移植文件，会被忽略；
符号链接仍会被拒绝。如果某个 Claude 历史路径只是把编码后的 Claude 存储键拼接在另一个已发现
项目目录之下，Codelux 会忽略该伪路径；正常的嵌套项目目录仍然保留。

交互式同步会分别确认是否允许覆盖 Providers、Claude 历史、Codex 历史、项目环境（包括已选择
的本地覆盖）、用户级 Agent 环境和 Agent 记忆。回答 `y` 只为该项已选择范围授予覆盖权限。显式
`--overwrite` 仍是非交互式的“覆盖全部已选范围”选项；只有确认所有已选目标都可被替换时才应
使用。

共享项目白名单包括分层 `AGENTS.md`、`AGENTS.override.md`、`CLAUDE.md`、项目内 Claude
导入文件、`.mcp.json`、`.worktreeinclude`、Claude 的
settings/rules/skills/agents/commands/hooks/workflows/agent-memory，以及 Codex 的
配置/rules/hooks/自定义 agents。用户白名单还包括 Claude keybindings/themes、直接 Codex
`hooks.json`、Codex 自定义 agents，以及兼容路径 `~/.codex/skills` 下除内置 `.system`
目录之外的用户技能。`CLAUDE.local.md`、`.claude/settings.local.json` 等仅本地文件必须
显式使用 `--local-project-env`。非交互式同步多个项目时，重复提供
`--project-map SOURCE=TARGET`；映射是显式的，不依赖参数顺序。离线导入只暴露不透明项目
ID，并使用 `--target-project PROJECT_ID=TARGET` 指定目标。

认证数据库、OAuth/账号状态、Provider 路由、Codex trust 和用户级 Codex MCP server 表
不会同步。JSON 中疑似秘密的字段会被移除；如果 MCP command 参数数组包含凭据参数名或
已识别的令牌前缀，整个参数数组都会被清空，需要在目标机器重新配置该命令。自由格式的
指令和命令仍可能包含私密内容，因此传输前应审查所选文件，并使用加密导出或可信 SSH 对端。

导入 Codex 用户设置时会进行合并：可移植设置可以传输，但目标机器会保留当前 Provider 路由和
trust 条目。Provider/配置快照继续存放在 `~/.codelux/backups`；会话历史和其他传输 payload 的
回滚数据则使用独立的 `~/.codelux/sync-transactions`，只暂存实际会变化的文件，并在传输成功后
删除临时回滚 payload。正常的状态和切换命令只读取轻量 manifest 元数据，不再扫描并校验存储的
payload 文件。

将 Codex 切回 `official` 时，有效的 ChatGPT 登录快照优先于 API key 快照。如果某个 key 已注册
给自定义 Provider，即使它的路由条目缺失，也不会仅因此被分类为官方 OpenAI API key。

`--memory` 范围包含所选项目的 Claude Markdown 自动记忆，以及 `~/.codex/memories` 下完整的
Codex 生成记忆；传输前应审查生成内容。同步会遵循 `CLAUDE_CONFIG_DIR` 和
`CLAUDE_CODE_PROJECT_DIR_NAME`。任意 `autoMemoryDirectory` 外部路径、所选项目之外的绝对路径或
主目录导入、原始插件缓存/数据、机器 trust 决定和 `/etc` 托管策略仍明确排除。

可以对任意命令使用 `--help` 查看当前选项和安全提示。

## AI 协同开发

Codelux 采用人类引导的开发者与 AI 编码 Agent 协同开发模式。实现、复审和治理角色按任务
分配：重要变更会在条件允许时进行独立复审，各项结论需要可复现的测试与检查作为证据，
项目方向、安全边界、合并和发布的最终责任仍由人类维护者承担。

我们欢迎更多开发者和 AI Agent 加入项目。你可以从 Issue、代码复审、测试、文档改进或
范围明确的 Pull Request 开始参与。提交贡献时，请清楚说明目标范围、验证证据，以及相关的
安全或隐私假设，帮助人类与 AI 协作者可靠地评估变更。

## 当前支持的系统环境

- macOS 12 或更高版本，支持 Intel 和 Apple Silicon
- 安装 Python 3.9–3.12 的 Linux 发行版
- Claude Code 和 Codex 已安装，并且命令位于 `PATH`
- 可写的用户主目录，用于客户端配置和 Codelux 状态文件

当前暂不支持 Windows 作为运行环境。Codelux 仍处于 Alpha 阶段，正式使用前请在目标环境验证具体的 Claude Code/Codex 版本和 Provider API 兼容性。

## 许可证

MIT
