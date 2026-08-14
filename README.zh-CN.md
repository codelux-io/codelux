# Codelux

[English](README.md) | [简体中文](README.zh-CN.md)

Codelux 是一个用于在多种 AI 编程助手之间管理大模型 Provider 配置的命令行工具，提供
明确的 Provider 切换、可恢复的本地修改和加密跨机器同步。

**官网与 Provider API：** [https://codelux.io](https://codelux.io)

## 主要能力

- 管理 Claude Code 和 Codex 的 Provider
- 只读检查配置与健康状态
- 通过快照恢复官方配置
- 对未完成的本地事务执行失败关闭式恢复
- 加密离线导入导出和基于 OpenSSH 的同步
- 对同步配置和会话数据执行明确的冲突处理

## 安全模型

Codelux 将 Provider 凭据保存在受操作系统文件权限保护的本地文件中。这些文件并未进行
静态加密，无法防御以同一用户身份运行的进程、特权管理员、恶意软件、账号失陷、备份
泄露或物理磁盘访问。

跨机器归档使用用户提供的密码加密。除非用户明确要求，否则不会在另一台机器上应用
活动客户端配置。

漏洞报告方式和详细安全边界见 [SECURITY.zh-CN.md](SECURITY.zh-CN.md)。

## 安装

Codelux 当前处于 Alpha 阶段，尚未发布到正式 Python Package Index。开发环境可从当前
代码目录安装：

```bash
python3 -m pip install -e .
```

## 使用方法

```bash
codelux version
codelux status --client claude
codelux list
codelux add codelux-io --url https://codelux.io --client claude
codelux switch codelux-io --client claude
codelux switch official --client claude
codelux recover --dry-run
```

同步命令支持明确选择传输内容：

```bash
codelux sync export --output codelux-sync.enc --providers --sessions
codelux sync import codelux-sync.enc
codelux sync push --ssh user@host.example --providers
codelux sync pull --ssh user@host.example --providers
```

可以对任意命令使用 `--help` 查看当前选项和安全提示。

## 开发验证

```bash
python3 -m pip install . pytest==7.4.4 coverage==7.10.7
coverage run -m pytest -q
coverage report
python scripts/check_public_repository.py
```

## 许可证

MIT
