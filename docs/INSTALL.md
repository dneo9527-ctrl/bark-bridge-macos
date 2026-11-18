# 安装、接入与维护

[← 返回项目首页](../README.md)

当前为 **2.6.1 正式版 · Build 49**，已封装为原生 macOS `.app`，有菜单栏铃铛与独立设置窗口。这是 GitHub 下载分发，未上架 App Store。Apple Silicon 图形安装助手及源码安装器只支持首次安装，不覆盖已有安装；2.6.0+ 客户端另有签名程序更新。

## 下载图形安装包

1. **下载 [Bark-Bridge-2.6.1-macOS-arm64.zip](https://github.com/dneo9527-ctrl/bark-bridge-macos/releases/download/v2.6.1/Bark-Bridge-2.6.1-macOS-arm64.zip)**（约 32 MB；精确大小与摘要见 Release），完整解压后先阅读 `Start Here.html`。
2. 打开 `Install Bark Bridge.app`，查看安装位置与 Codex 接入选项，再点击“安装并打开”。此包内置 Python，无需自行安装 Xcode、Python 或 pip。
3. 安装完成后添加接收设备，在 iPhone 上确认收到测试。未找到 Codex CLI 时可先安装 App，稍后按下文接入。

| 附件 | 使用者与用途 |
| --- | --- |
| `Bark-Bridge-2.6.1-macOS-arm64.zip` | 新用户首次安装；含已封装的 App、图形助手及 Python 3.13.18，不需要终端、Xcode 或编译 |
| `Bark-Bridge-2.6.1-macOS-arm64-update.zip` | 已启用 2.6+ 更新客户端的用户；由客户端验证同 Release 的 JSON 清单及 Ed25519 `.sig`，不要手动覆盖程序 |
| `Bark-Bridge-2.6.1-source.zip` | 开发者审查/自行构建；不是普通用户的首次安装包 |

当前安装包只分发 Apple Silicon（M 系列）。编译最低目标 macOS 13，实际界面仅在 macOS 26 arm64 本机验证；没有宣称 Intel 或 macOS 13 实机通过。下方“获取源码”和“环境与构建”仅面向开发者，图形包用户不需要执行。

**Apple 签名状态：** 此包采用 ad-hoc 签名，尚未使用 Developer ID 签名或 Apple 公证。macOS 可能拦截首次打开；请先核对本项目的下载来源和 `SHA256SUMS`，再阅读 [Apple 安全打开说明](https://support.apple.com/102445) 并自行决定是否信任。不移除隔离标记、不关闭 Gatekeeper。发布者的 Ed25519 更新签名不能替代 Apple 公证。

安装到 `/Applications/Bark.app`，共用运行目录为 `~/Library/Application Support/CodexBark`；需要当前用户对 Applications 有写权限，不自动请求 sudo。已有 App、运行目录或指向它们的符号链接都会阻止安装。安装助手不会升级已有用户版本。

2.6.2 开发候选支持安装后移动实际 App 或改名，共用运行目录保留在原位置；登录入口和程序更新会跟随 App。首次升级后需先打开一次完成登记，适用范围与限制见[移动说明](RELOCATION.md)。公开 2.6.1 尚不包含此改动。

嵌入式 Python 固定为 python-build-standalone 3.13.18（20261110），下载哈希见 `assets/python-runtime.json`。运行时和许可一起复制到共用目录，Hook、Skill、菜单及面板优先使用它。源码首次安装仍可使用系统 Python。

## 已有 2.4.x / 2.5.x 安装

这些版本没有更新公钥入口，当前没有面向旧版的一键迁移工具。请继续使用原版，不用首次安装助手覆盖，也不手动复制未知程序文件。需要迁移指导时，可在 [Issues](https://github.com/dneo9527-ctrl/bark-bridge-macos/issues) 提供版本号、macOS 版本与架构；不要上传设备地址、配置、日志、聊天或备份。维护者核对旧安装结构后再提供针对性的备份与回退步骤。本机一次受控源码迁移成功不代表所有旧安装已验证。

## 已有 2.6+ 安装的更新

在“设置”检查更新或选择自动下载，后者默认关闭；开启后每 12 小时按需检查，菜单启动/系统唤醒也受时间门限约束。校验下载完成后点击“重启并更新”才安装，不会在工作途中自行重启。设备数据、既有 Hook/Skill 接入与授权策略保留；完整机制见 [软件更新](UPDATES.md)。首次安装 ZIP、程序更新 ZIP 与源码 ZIP 不互相替代。

## 配置 iPhone 接收

iPhone 安装 Bark 并允许通知，Mac 打开菜单栏铃铛的“设置”→接收设备，填入手机 Bark 的 HTTPS 地址、保存并发送测试。地址/密钥只在本机填写，不提交到 Issue、聊天或截图。Wi-Fi 或移动网络均依赖手机联网、APNs 与推送服务；离线不保证实时收到，发送网关接受不等于手机实收。Bark Bridge 仅响应已接入的事件，不会自动转发任意 App 的全部通知。

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

只生成当前架构的 `build/Bark.app`，不会安装或启动 App。签名为 ad-hoc，未使用 Developer ID 签名或 Apple 公证。想直接试用时可使用上方的 Apple Silicon 安装包。

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

安装器只支持首次安装；发现已有 Bark App 或共用运行目录会停止，防止覆盖其他安装。构建或测试不会调用安装器，也不触碰现有手机配置。2.6.0 起的设置页可检查更新和选择自动下载，下载校验后由用户确认重启安装，见 [软件更新](UPDATES.md)。较早版没有这个入口，需先按维护者指导备份并启用可信客户端；不能用首次安装助手覆盖升级。

如果首次安装中途失败，安装器可能已移除 App，但会保留共用运行目录和失败备份；不要反复运行 `--apply`。修正 CLI 路径或其他错误后，先把失败的共用目录移到另一安全位置保留备份，确认 `/Applications/Bark.app` 不存在，再重新首次安装。该步骤只适用于已失败且未投入使用的安装，不要移动仍在使用的运行目录。

打开面板管理接收设备；也可点击屏幕右上角菜单栏铃铛 → “设置…”。在隐藏输入框中填写这台手机 Bark 首页的 HTTPS 推送地址，保存后发送测试并在手机确认收到。地址仅保存在本机受限权限文件中，不需要发到聊天、填入 Hook 或提交 GitHub。手机配置只填一次。

Codex CLI 从 PATH 或常见 App 路径发现。若安装在其他位置，显式设置可执行文件路径后再运行接入：

```sh
CODEX_CLI_BIN="/absolute/path/to/codex" "$HOME/Library/Application Support/CodexBark/python/bin/python3" "$HOME/Library/Application Support/CodexBark/manage.py" attach "$HOME/.codex"
```

下文命令使用安装包内置的 Python。若从源码安装且没有 `python/bin/python3`，用 `/usr/bin/python3` 替换命令中的解释器路径。

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

