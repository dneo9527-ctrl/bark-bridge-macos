# 软件更新

2.6.0 起，设置页显示安装版本与 Build，提供检查更新、自动下载、GitHub 和更新日志入口。

- 自动下载默认关闭。开启后，菜单进程启动、系统唤醒及每 12 小时按需检查；后台检查有时间门限，失败静默。无独立常驻更新服务。
- 下载完成后点击“重启并更新”才安装。不会在工作途中自行重启。菜单未运行时不进行后台检查，仍可在面板手动检查。
- 预览版接收稳定版和 preview 更新，稳定版只接收稳定更新。只选择具有本机架构签名清单的 Release，现有旧版首次安装 ZIP 不会被当成更新包。
- 检查使用固定公开仓库的 GitHub API，不读取 GitHub 登录或令牌。限流、断网和校验失败会显示可重试状态，不影响推送。

## 2.6.2 preview.3 · Build 52 预览

此预览保留同卷移动 App 后的位置追踪，改为接入原生 Antigravity App 官方 Hook。它的版本为 `2.6.2`，标签为 `v2.6.2-preview.3`，通道为 `preview`，不会替代 `v2.6.1` 稳定发行。旧 Build 51 的 preview.2 草稿保留停发。

**稳定版客户端只查稳定更新，因此不会自动显示此预览。** 已有预览客户端可通过签名更新流程选择符合条件的新版本；稳定版用户如需试用，先按维护者指导显式进入预览。首次安装助手只接受没有旧 App／共用运行目录的 Mac，不能用来覆盖升级。不要删除配置、手动解压 update.zip 覆盖程序或改写已签名 App 的通道文件。

程序更新后需先打开一次，登记实际 App 的书签。移动范围及限制见 [RELOCATION.md](RELOCATION.md)，原生 App 接入和未通过的现场验证见 [ANTIGRAVITY.md](ANTIGRAVITY.md)，不需要自建 Console。

## 2.6.1 · Codex 来源图标与手机历史

`v2.6.1`、Build 49、stable 通道。Codex 的授权、提问、本轮完成及来源测试通知改用 Codex 自身图标，并显式开启手机 Bark 历史保存。之前发送器设置 `isArchive=0`，通知可以弹出但不会保存到 Bark 历史；旧漏存消息无法补回。仅按 `source_id=codex` 生效，其他来源及旧四参数调用沿用原策略。

关闭手机内容预览时，历史只保存发送的通用文案；开启预览时可保存聊天名及问题摘要。可在手机 Bark 管理或删除已保存记录，详情见[隐私说明](PRIVACY.md)。来源图标固定到不可变的公开 PNG，权利归 OpenAI，见[图稿说明](THIRD_PARTY_ARTWORK.md)。Bark 远程图标要求 iOS 15+，本机用户已确认一台 iPhone 的图标和历史记录；其他设备与图源失败回退未测。网关接受不能证明手机已展示。

已有 2.6+ 客户端使用“检查更新”，下载校验后手动重启安装。首次安装包仅供没有旧安装的 Mac。未新增 Mac 图片下载、重试或常驻服务，路由、静音、勿扰、事件开关及授权策略保持。

## 2.6.0 历史正式版

正式版为 `v2.6.0`、Build 47、stable 通道；已安装 `v2.6.0-preview.1` 的用户可在设置中检查更新。稳定版按标签和 GitHub prerelease 标记过滤预览更新。此前预览版标签与附件保留；不要用首次安装包覆盖已有安装。

## 保留与回退

更新协议 1 只替换当前安装的 Bark App 和共享目录中的 `bark.py`、`manage.py`、`claude_bark.py`、`bark-run`、`updater.py`。首次安装默认位置是 `/Applications/Bark.app`；2.6.2 开发候选会记住移动后的实际 App，详见[移动说明](RELOCATION.md)。设备、规则、来源、历史、Hook/Skill 入口、Codex 账号目录与授权策略不在更新包范围内。Python 解释器不在协议 1 更新范围内，后续需要升级解释器时应另设计兼容迁移。

后台任务与推送路径独立。运行时文件通过原子替换更新，已加载的发送进程可以继续；不会停止 Codex，也不会强杀正在发送的进程。

更新前在共享目录 `updates/backup-…` 备份旧 App 与程序文件。App 通过同卷原子交换替换，程序文件逐个原子替换，事务保存在 `updates/transaction.json`。新菜单进程提交版本和 PID 回执后才标为完成；失败恢复备份。若更新助手被中断，下次启动菜单时恢复未完成事务并重新载入旧程序。备份保留用于回退，不是可随意清理的下载缓存。

需要人工回退时，先退出 Bark 面板和菜单，然后使用安装时的更新模块调用 `Updater(...).restore(transaction)`；仅对准确的未损坏备份操作，恢复后重新启动 Bark。不要删除设备目录或以首次安装助手覆盖它。通常启动时自动恢复即可，无需人工操作。

## 发布者流程

1. 运行测试和 `scripts/audit.py`，统一 VERSION 和 assets/release.json 中的 version、build、tag、channel，再 `python3 scripts/build.py`。
2. 首次建立独立 Ed25519 发布密钥：`xcrun swift scripts/sign-update.swift create /受限本机目录/update-signing.key assets/update-public-key.txt`。私钥只留本机，目录 0700、文件 0600；公钥进入 App。已有密钥不能重建或覆盖，应独立备份。密钥轮换需要兼容设计。
3. `python3 scripts/package-update.py --signing-key /受限本机目录/update-signing.key --out /新的输出目录`。只从公开源码与构建输出取程序文件，生成 ZIP、`bark-update-arm64.json`、`bark-update-arm64.sig`、SHA256SUMS。打包后不再改动构建产物。
4. 向对应 tag 的 Release 上传全部更新附件，确认签名与摘要、准确源码 SHA、CI 和重新下载验证。不得覆盖已发 tag/附件。首个包含公钥的版本需要先通过现有可信安装方式安装；更早版本没有更新入口。
5. 首次安装包仍用 `scripts/package.py` 构建，不能拿它升级已有数据。两个 ZIP 用途不同，在 Release 中分别标明。

签名清单固定仓库、tag、版本、架构、大小与 ZIP 摘要，并列出每个文件的摘要和模式。客户端使用 App 内置 CryptoKit 验证器核验 Ed25519；拒绝降级、未知协议、路径逃逸、符号链接、重复或额外文件，再验证 App 签名与可执行健康检查。

发布签名证明更新来自持有发布私钥的一方，不能替代 Apple Developer ID、Gatekeeper 或公证。当前 GitHub 正式分发仍是 ad-hoc 签名，尚未公证。不会移除隔离标记或关闭系统安全检查；macOS 拦截时参考 [Apple 安全打开说明](https://support.apple.com/102445)。

真实网络新版下载到重启、网络下载后隔离首开、Intel 和旧 macOS 的结果需分别验收，不能用隔离事务测试替代。
