# Bark Bridge for Mac

将 Mac 上需要处理的 Codex 提醒发送到 iPhone Bark。一个 macOS 用户共用一份手机配置，支持菜单栏开关、多设备和按来源路由。

这是独立制作的社区工具，与 Bark、OpenAI、Anthropic 无官方关联。

**2.4.1 · MIT 许可证 · 源码预发布。**

## 它解决什么问题

离开 Mac 后，Codex 可能正等你授权或回答。Bark Bridge 把这些需要处理的时刻推送到 iPhone，让你知道是哪个聊天需要关注；本轮完成时也可以提醒。

同一个 macOS 用户共用一份接收设备配置。现有已接入目录切换 Codex 账号时，无需重复填写手机地址，iPhone 也无需跟着切换 ChatGPT 账号。新的独立 CODEX_HOME 仍需接入一次。

## 功能一览

| 场景或功能 | 能做什么 |
| --- | --- |
| 等待授权 | 提醒返回 Mac 查看请求并决定，包含聊天名和工具，不发送授权命令 |
| 等待回答或选择 | 支持的提问工具发送短问题和选项；普通文字提问由本地 Skill 尽力补充 |
| 本轮完成 | 主线程本轮回复结束后提醒，三类 Codex 提醒可分别开关 |
| 多台手机 | 记忆接收设备，独立启停，并按来源分配接收手机 |
| 安静时段 | 每日勿扰、全局暂停和单来源临时静音，跳过后不积压补发 |
| 场景预设 | “需要我时”“离开电脑”“专注片刻”组合现有提醒开关；不会自动判断你是否离开 |
| Mac 电池 | 未接电源时的低电量提醒，监听系统电源事件 |
| 终端与快捷指令 | 显式包装的长任务结束提醒，以及 Mac 快捷指令末尾调用模板 |
| 发送记录与诊断 | 查看来源、各设备的网关发送结果和脱敏错误类别 |
| Claude Code（实验性） | PermissionRequest、AskUserQuestion、Stop 适配；只完成配置合并与隔离测试，尚未验证真实会话 |

提醒示例为“示例聊天 · 等待授权”“示例聊天 · 等待回答”“示例聊天 · 本轮已完成”。支持的问题会带短摘要与选项；手机只负责显示，实际回答和授权仍在 Mac 完成。内容预览可在 App 中关闭。

```mermaid
flowchart LR
    A[Mac 上的应用事件] --> B[Bark Bridge 菜单栏工具]
    B --> C[Bark HTTPS 推送服务]
    C --> D[iPhone Bark 通知]
```

这是原生 macOS 菜单栏与设置面板工具。添加其他 .app 仅代表登记来源，应用仍须提供 Hook、脚本或明确的调用入口，不会自动转发全部 macOS 通知。

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

只生成当前架构的 `build/Bark.app`。不会安装或启动 App。签名为 ad-hoc，本候选未使用 Developer ID 签名或 Apple 公证。尚未提供可承诺直接下载安装的二进制发行包；请优先从源码在自己的 Mac 构建。

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
CODEX_CLI_BIN="/absolute/path/to/codex" /usr/bin/python3 "$HOME/Library/Application Support/CodexBark/manage.py" attach "$HOME/.codex"
```

已验证的 Codex 版本为 0.167.0-alpha.3.2；Hook 及信任接口可能随 Codex 更新变化。接入前备份原配置，合并四条提醒 Hook，精确信任本工具的 Hook 定义；不改变 Codex 操作授权策略，不批准操作。旧聊天可能需要重新载入 Skill。

CC Switch 共用配置合并默认关闭。确需跨其账号配置轮换时，向 `attach` 或安装器额外传入 `--with-cc-switch`；这会显式修改其本机 common TOML 配置，并先保存备份，不读取账号凭据。

## 多账号与多个配置目录

同一 macOS 用户、同一已接入 CODEX_HOME 换号，继续引用共用发送器和设备配置。独立目录需分别接入一次：

```sh
/usr/bin/python3 "$HOME/Library/Application Support/CodexBark/manage.py" attach "/absolute/path/to/other-codex-home"
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
/usr/bin/python3 "$HOME/Library/Application Support/CodexBark/bark.py" template-notify --template shortcuts
```

只有工作流执行到该动作才推送。这条路径适用于 Mac，不能放到 iPhone 上直接执行。没有自动创建或执行快捷指令。

Claude Code 模板先检查本机 CLI。缺少 CLI 时不会代装或生成设置；接入时合并本工具入口，保留其他 Hook、permissions 和 env，不复制账号配置。移除来源会先清理自己的入口；失败时保留来源并提示。

## 隐私、资源与边界

参见 [隐私说明](docs/PRIVACY.md) 和 [验证范围](docs/VERIFICATION.md)。

- 共用数据位于 `~/Library/Application Support/CodexBark`；目录 0700、手机配置 0600。
- 不读取账号令牌或聊天正文；名称来自本地名称元数据。支持事件只发送聊天名、短问题/选项及固定文案，可在 App 关闭内容预览。
- 2.4.1 修复了 Hook 会话 ID 与聊天 ID 不同时的名称回退，并补充有界名称索引读取。无法确定名称时仍保留短编号，避免串用其他聊天名称。
- 失败不阻塞 Codex，不自动批准，无新网络轮询或长期重试。菜单/电池服务常驻，设置面板关闭后退出。
- 服务器接受推送不等于 iPhone 实收；Mac 睡眠、关机、离线、APNs 或手机专注设置都可能影响送达。锁屏与系统睡眠是不同状态。
- 手机用于查看提醒，回答和审批仍回到 Mac。

## 卸载

在 App 的“设置 → 高级 → 卸载”执行，或：

```sh
/usr/bin/python3 "$HOME/Library/Application Support/CodexBark/manage.py" uninstall
```

会删除本工具入口、手机配置与通知状态，保留其他 Codex/Claude 设置和备份。Claude 入口清理失败会停止整体卸载，避免留下失效 Hook；可检查本地 Claude 设置后重试。卸载后重启 Codex。共用代码和备份目录保留供检查，不要把整份旧配置覆盖回去。若已成功卸载并希望重新安装，先将保留的共用目录移到另一安全位置保存备份，再使用首次安装器；不要直接删除仍在使用的运行目录。

## 许可

本项目采用 [MIT 许可证](LICENSE)，允许使用、修改、再分发及商用，须保留版权与许可声明。图标由本项目的 AI 设计流程生成并适配，不来自 Bark 官方素材。
