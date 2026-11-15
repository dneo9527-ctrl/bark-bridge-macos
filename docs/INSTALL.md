# 安装、接入与维护

[← 返回项目首页](../README.md)

当前为 **2.5.0 开发者安装预览**。提供 Apple Silicon 图形安装包及源码构建两种方式；都只支持首次安装，不覆盖已有安装。

## 下载图形安装预览

1. 下载 [Release 中的 Apple Silicon ZIP](https://github.com/dneo9527-ctrl/bark-bridge-macos/releases/tag/v2.5.0-preview.1)，解压后先阅读 `Start Here.html`。
2. 打开 `Install Bark Bridge.app`，查看安装位置与 Codex 接入选项，再点击“安装并打开”。此包内置 Python，无需自行安装 Xcode、Python 或 pip。
3. 安装完成后添加接收设备，在 iPhone 上确认收到测试。未找到 Codex CLI 时可先安装 App，稍后按下文接入。

**未公证预览：** 此包采用 ad-hoc 签名，尚未使用 Developer ID 签名或 Apple 公证。macOS 可能拦截首次打开；请先核对本项目的下载来源和 `SHA256SUMS`，再阅读 [Apple 安全打开说明](https://support.apple.com/102445) 并自行决定是否信任。无需关闭 Gatekeeper、移除整个系统的安全保护或禁用检疫。

安装到 `/Applications/Bark.app`，共用运行目录为 `~/Library/Application Support/CodexBark`；需要当前用户对 Applications 有写权限，不自动请求 sudo。已有 App、运行目录或指向它们的符号链接都会阻止安装。安装助手不会升级已有用户版本。

嵌入式 Python 固定为 python-build-standalone 3.13.18（20261110），下载哈希见 `assets/python-runtime.json`。运行时和许可一起复制到共用目录，Hook、Skill、菜单及面板优先使用它。源码首次安装仍可使用系统 Python。

## 获取源码

```sh
git clone https://github.com/dneo9527-ctrl/bark-bridge-macos.git
cd bark-bridge-macos
```

也可以在仓库页面使用 **Code → Download ZIP**，解压后在该目录中运行下文命令。

## 环境与构建

仅在 Apple Silicon Mac、macOS 26 上实测。构建目标为 macOS 13+；旧系统及 Intel Mac 尚未实机验证。

需要 Xcode 或 Xcode Command Line Tools，以及 `/usr/bin/python3`、`curl`；测试还使用系统 OpenSSL。Python 仅使用标准库，无 pip 依赖。

```sh
/usr/bin/python3 scripts/test.py
/usr/bin/python3 scripts/build.py
```

只生成当前架构的 `build/Bark.app`，不会安装或启动 App。签名为 ad-hoc，未使用 Developer ID 签名或 Apple 公证。想直接试用时可使用上方的 Apple Silicon 安装预览。

每次提交会运行 [GitHub Actions 测试与构建](https://github.com/dneo9527-ctrl/bark-bridge-macos/actions/workflows/checks.yml)，实际结果以对应提交的检查记录为准。

## 首次安装

先在 iPhone 安装 Bark 并允许通知。随后在 Mac 构建并预览安装计划：

```sh
/usr/bin/python3 scripts/install.py
```

安装前请确认 Codex CLI 已安装，且能通过 PATH 或常见 App 路径找到。若安装在其他位置，请在这次安装命令前设置 `CODEX_CLI_BIN="/absolute/path/to/codex"`（下方也提供单独接入命令）。

明确安装并接入一个 Codex 配置目录：

```sh
/usr/bin/python3 scripts/install.py --apply --codex-home "$HOME/.codex"
open /Applications/Bark.app
```

安装器只支持首次安装；发现已有 Bark App 或共用运行目录会停止，防止覆盖其他安装。构建或测试不会调用安装器，也不触碰现有手机配置。未提供自动更新功能。

如果首次安装中途失败，安装器可能已移除 App，但会保留共用运行目录和失败备份；不要反复运行 `--apply`。修正 CLI 路径或其他错误后，先把失败的共用目录移到另一安全位置保留备份，确认 `/Applications/Bark.app` 不存在，再重新首次安装。该步骤只适用于已失败且未投入使用的安装，不要移动仍在使用的运行目录。

打开面板管理接收设备；也可点击屏幕右上角菜单栏铃铛 → “设置…”。在隐藏输入框中填写这台手机 Bark 首页的 HTTPS 推送地址，保存后发送测试并在手机确认收到。地址仅保存在本机受限权限文件中，不需要发到聊天、填入 Hook 或提交 GitHub。手机配置只填一次。

Codex CLI 从 PATH 或常见 App 路径发现。若安装在其他位置，显式设置可执行文件路径后再运行接入：

```sh
CODEX_CLI_BIN="/absolute/path/to/codex" "$HOME/Library/Application Support/CodexBark/python/bin/python3" "$HOME/Library/Application Support/CodexBark/manage.py" attach "$HOME/.codex"
```

下文命令使用安装预览内置的 Python。若从源码安装且没有 `python/bin/python3`，用 `/usr/bin/python3` 替换命令中的解释器路径。

已验证的 Codex 版本为 0.167.0-alpha.3.2；Hook 及信任接口可能随 Codex 更新变化。接入前备份原配置，合并四条提醒 Hook，精确信任本工具的 Hook 定义；不改变 Codex 操作授权策略，不批准操作。旧聊天可能需要重新载入 Skill。

CC Switch 共用配置合并默认关闭。确需跨其账号配置轮换时，向 `attach` 或安装器额外传入 `--with-cc-switch`；这会显式修改其本机 common TOML 配置，并先保存备份，不读取账号凭据。

## 多账号与多个配置目录

同一 macOS 用户、同一已接入 CODEX_HOME 换号，继续引用共用发送器和设备配置。独立目录需分别接入一次：

```sh
"$HOME/Library/Application Support/CodexBark/python/bin/python3" "$HOME/Library/Application Support/CodexBark/manage.py" attach "/absolute/path/to/other-codex-home"
```

不要在其他账号、电脑或云端会话上假定本机 Hook 自动存在。新平台、新版本和其他账号管理器未经验证。

## 接入终端、快捷指令与 Claude Code

在“来源 → 内置模板”中选择。终端示例：

```sh
"$HOME/Library/Application Support/CodexBark/bark-run" /usr/bin/true
```

用自己的程序和参数替换 `/usr/bin/true`。保留 stdout、stderr 和退出码；只发送成功/失败，不发送命令与输出。

Mac 快捷指令的末尾“运行 Shell 脚本”动作：

```sh
"$HOME/Library/Application Support/CodexBark/python/bin/python3" "$HOME/Library/Application Support/CodexBark/bark.py" template-notify --template shortcuts
```

只有工作流执行到该动作才推送。这条路径适用于 Mac，不能放到 iPhone 上直接执行。没有自动创建或执行快捷指令。

Claude Code 模板先检查本机 CLI。缺少 CLI 时不会代装或生成设置；接入时合并本工具入口，保留其他 Hook、permissions 和 env，不复制账号配置。移除来源会先清理自己的入口；失败时保留来源并提示。

## 卸载

在 App 的“设置 → 高级 → 卸载”执行，或：

```sh
"$HOME/Library/Application Support/CodexBark/python/bin/python3" "$HOME/Library/Application Support/CodexBark/manage.py" uninstall
```

会删除本工具入口、手机配置与通知状态，保留其他 Codex/Claude 设置和备份。Claude 入口清理失败会停止整体卸载，避免留下失效 Hook；可检查本地 Claude 设置后重试。卸载后重启 Codex。共用代码和备份目录保留供检查，不要把整份旧配置覆盖回去。若已成功卸载并希望重新安装，先将保留的共用目录移到另一安全位置保存备份，再使用首次安装器；不要直接删除仍在使用的运行目录。

