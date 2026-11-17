# 验证范围

## 2.6.0 GitHub 正式版 · Build 47

本节对应已公开的 [`v2.6.0` 正式发行](https://github.com/dneo9527-ctrl/bark-bridge-macos/releases/tag/v2.6.0)，stable 通道，Build 47。2026-11-17 对齐发布后的核验记录；下方预览版构建信息仅属于历史版本。

| 发行身份 | 已核对的值 |
| --- | --- |
| 发布源码提交与标签目标 | [`72716094c7d126afb19cace7b3341665738f4b97`](https://github.com/dneo9527-ctrl/bark-bridge-macos/commit/72716094c7d126afb19cace7b3341665738f4b97) |
| Git tree | `c613f1e64ef7a7eaab018d71c1a283c5b5e6c945`；80 个源码文件与发行源码 ZIP 的字节及模式一致 |
| 准确提交的 CI | [43835538361](https://github.com/dneo9527-ctrl/bark-bridge-macos/actions/runs/43835538361)，已成功：隔离测试、源码审计、安装助手类型检查和原生构建 |
| 配套组件 | 首次安装包内含 App、Python、许可、`SKILL.md.in` 与 `runtime/manage.py`；Codex Hook/Skill 接入入口及说明随包提供 |

系统 Python 3.9.6 与安装包内置 Python 3.13.18 各执行 283 项测试，各通过 282 项、跳过 1 项可选真实 CLI 测试。另在临时 CODEX_HOME 使用真实 Codex CLI 验证首次安装与四条可信 Hook，保留原授权和第三方 Hook；不读取或修改现用手机配置。

首次安装包的 App、Python、许可及程序来源已核对；原生构建、深层 ad-hoc 签名、隔离首次安装、重复安装拒绝通过。实际公开的 `v2.6.0-preview.1` App、更新器和验签器在临时安装中接受正式签名清单，完成 Build 46 到 47 的程序替换及 stable 通道切换，假设备/规则/入口哨兵保持不变。本地冻结附件检查另包含篡改清单拒绝。

公开发布后，已使用真实公开预览客户端，从匿名 GitHub 地址检查正式版、下载更新包并验签，在临时目录完成 Build 46→47；更新事务完成、深层签名检查通过，假用户数据未变，升级后的 stable 客户端再次检查返回 `current`。这轮使用真实网络，但仍是临时 App/运行目录、合成数据及 `apply(restart=False)`，未启动或重启真实安装，不代表完整界面重启或手机实收验收。

六个附件在草稿阶段重新下载，随后又从匿名公开 URL 下载；本地文件、GitHub digest 和下载文件的大小及 SHA-256 一致，三个 ZIP 的 CRC 检查通过。发行时的附件记录如下，也可下载同一 Release 的 `SHA256SUMS` 核对：

| 附件 | 字节数 | SHA-256 |
| --- | ---: | --- |
| `Bark-Bridge-2.6.0-macOS-arm64.zip` | 31966208 | `a4374f54cbc104623027b3489ce91c31eaa176776766d4051e8cd7d6ff4fa43c` |
| `Bark-Bridge-2.6.0-macOS-arm64-update.zip` | 3220061 | `d2f4eccb0a8f240fcf73f46972632118fe39fb1e5de07d9e50299f9d9dbda3c4` |
| `Bark-Bridge-2.6.0-source.zip` | 1788408 | `3bce14da535877778383bbe61acd4576c0f8bd685614423a07ddf9b9e0bed42b` |
| `bark-update-arm64.json` | 2622 | `c17b241c517e08b59bd8f3ff48046e4bfc5823fb9379a7a5fb56a446d851f765` |
| `bark-update-arm64.sig` | 89 | `b2afdc1e50c9a6ee096b9a1c4252f56de47ec6f9664ae1b9a866fe5becf3e6da` |
| `SHA256SUMS` | 479 | `2def9b13600a47e0be669eeb116f50fd31ea4e454e071c55c45caf02861ce4ad` |

稳定发行沿用 ad-hoc 签名，无 Developer ID 或 Apple 公证。全新实体 Mac 下载隔离首开、额外手机实收、真实 Claude Code 会话、Intel、macOS 13 实机、真实低电量放电、长期耗电及长期睡眠/离线稳定性尚未验收。首次安装助手拒绝覆盖旧安装，未提供覆盖所有 2.4.x/2.5.x 安装的一键迁移工具；旧版维护步骤见[安装说明](INSTALL.md#已有-24x--25x-安装)。更新清单签名不等于 Apple 公证，网关接受也不等于手机实收。

本页为发行后的文档订正；原始发布源码和附件保留于 `v2.6.0`，其内文档反映发行时状态。以下各节为早期版本历史记录，不替代本节 Build 47 的验证。

## 2.4.0 / 2.4.1 历史记录

本机原版 2.4.0 的 Swift/AppKit 面板、Objective-C 菜单、电池事件接口和发送器已经安装验证。终端包装器与快捷指令调用均实际得到 Bark 网关 HTTP200 接收。没有自动创建或执行完整的 macOS 快捷指令工作流。

2.4.1 本机补丁共通过 253 次 Python 测试执行（含继承重复回归），其中 14 项为新增聊天身份与索引边界用例。真实本机名称解析已验证；原截图中那条历史通知的完整 Hook 输入未保存，不能据此断言它当时一定来自分支或临时会话。新的完成测试遵守用户当前关闭设置而被跳过，不将其算作手机实收验证。

公开源码候选另行运行仓库测试与本机构建，具体结果见仓库 release-check.json。测试仅使用仓库源码和临时文件；真实 Codex CLI 的隔离接入测试默认跳过，只有显式 BARK_RUN_CODEX_INTEGRATION=1 时启用。远端 CI 结果请查看 [对应提交的 Actions 记录](https://github.com/dneo9527-ctrl/bark-bridge-macos/actions/workflows/checks.yml)，本地测试结果不替代远端检查。

尚未验证：真实 Claude Code 会话、Intel Mac、macOS 13 上的实机表现、第二实体手机、公开下载后二进制 Gatekeeper 行为、Developer ID 公证、长期耗电和新版本长期睡眠/离线稳定性。本候选不承诺这些未测场景。


## 2.5.0 安装预览

新增 Apple Silicon 图形首次安装助手，内嵌 python-build-standalone 3.13.18。下载归档先校验固定 SHA-256，保留上游 Python 与依赖许可；包体从公开源码与固定运行时构建，不读取现有安装的手机配置。安装目标有已有文件或符号链接时停止。

隔离目录已实际完成：复制 App 与 Python、执行内置 Python 状态查询、验证 App 深层签名、拒绝二次安装；真实本机 Codex CLI 对临时 CODEX_HOME 合并并信任四条 Hook，保留原授权策略及第三方 Hook；权限事件入口返回空输出，不产生审批决定。其他测试涵盖失败时保留备份、部分 App 拷贝清理及双解释器 Hook 清理。

安装包采用 ad-hoc 签名，`codesign --verify --deep --strict` 成功；`spctl --assess` 拒绝，符合未公证预览的状态，不将本机构建可运行等同于网上下载后可直接打开。没有 Developer ID、公证及全新 Mac 隔离属性后的首次打开实测。旧 macOS、Intel 和完整新用户首次手机送达仍未验证。

30 秒演示是 1280×720 H.264 无声字幕动画，使用虚构聊天与手机界面，全片标注功能示意；不作为实际 App 录屏或手机实收证据。

## 2.6.0-preview.1 软件更新预览 · Build 46 历史记录

本轮系统 Python 与内置 Python 3.13 各执行 280 项测试，各通过 279 项、跳过 1 项可选真实 Codex CLI 用例。新增 14 项更新测试覆盖后台默认关闭/时间门限、稳定版与预览版选择、签名拒绝、ZIP 路径/额外文件/链接/摘要、事务各阶段回退、模拟中断后恢复及用户数据保留。原生 AppKit/Objective-C/CryptoKit 构建与深层 ad-hoc 签名验证通过，设置页离屏图检查无裁切。

签名程序包的原生验签、篡改拒绝、解包、完整性、隔离升级和假数据保留已验证。本机由 2.4.1 升级到 2.6.0 预览版成功，菜单回传准确版本与 PID，实际设置 UI 已核对。首次本机尝试暴露旧菜单路径识别问题并自动回退；修复后再升级成功，已补回归。未读取或复制真实手机配置。该预览版发布前的本地记录见其 release-check.json，当时尚未包含公开网络升级及远端 CI 结果；正式版 Build 47 的 CI 与公开升级结果见本页首节。隔离首开、Intel 和旧 macOS 实机仍未验收。更新清单签名不等于 Apple 公证。

仅适用于 [`v2.6.0-preview.1`](https://github.com/dneo9527-ctrl/bark-bridge-macos/releases/tag/v2.6.0-preview.1) 的发布对应关系：该预览版的首次安装包、签名程序更新包与同名 `Bark-Bridge-2.6.0-source.zip` 来自构建提交 `45d4034a8a833b03dc5f63eae1270fc075108936`。预览发布分支只更新 README、安装说明、版本徽标、验证说明及 CI 固定依赖；程序与已签附件不变。预览标签对应的源码归档另包含这些发布文案，实际 CI 与附件核验记录在该预览 Release 说明中。该旧构建提交不是正式版 Build 47 的源码身份。
