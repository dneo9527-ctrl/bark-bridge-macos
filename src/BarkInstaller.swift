import AppKit
import Foundation

let targetApp = URL(fileURLWithPath: "/Applications/Bark.app")
let targetRuntime = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support/CodexBark")
let payload = Bundle.main.resourceURL!.appendingPathComponent("Payload")

func codexBinary() -> URL? {
    let fm = FileManager.default
    if let override = ProcessInfo.processInfo.environment["CODEX_CLI_BIN"] {
        let path = (override as NSString).expandingTildeInPath
        guard path.hasPrefix("/"), fm.isExecutableFile(atPath: path) else { return nil }
        return URL(fileURLWithPath: path)
    }
    var candidates: [URL] = []
    for folder in ["/Applications", fm.homeDirectoryForCurrentUser.appendingPathComponent("Applications").path] {
        for app in ["Codex.app", "ChatGPT.app"] {
            for suffix in ["Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex", "Contents/Resources/codex"] {
                candidates.append(URL(fileURLWithPath: folder).appendingPathComponent(app).appendingPathComponent(suffix))
            }
        }
    }
    for folder in (ProcessInfo.processInfo.environment["PATH"] ?? "").split(separator: ":") where folder.hasPrefix("/") {
        candidates.append(URL(fileURLWithPath: String(folder)).appendingPathComponent("codex"))
    }
    return candidates.first { fm.isExecutableFile(atPath: $0.path) }
}

final class InstallerBackground: NSView {
    override func draw(_ dirtyRect: NSRect) {
        NSColor.windowBackgroundColor.setFill(); bounds.fill()
        super.draw(dirtyRect)
    }
}

final class Installer: NSObject, NSApplicationDelegate, NSWindowDelegate {
    var window: NSWindow!
    var installButton: NSButton!
    var attachBox: NSButton!
    var status: NSTextField!
    var spinner: NSProgressIndicator!
    var busy = false
    var installed = false
    let cli = codexBinary()

    func label(_ text: String, size: CGFloat = 13, weight: NSFont.Weight = .regular, secondary: Bool = false) -> NSTextField {
        let field = NSTextField(wrappingLabelWithString: text)
        field.font = .systemFont(ofSize: size, weight: weight)
        field.textColor = secondary ? .secondaryLabelColor : .labelColor
        return field
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        let checkOnly = CommandLine.arguments.contains("--check-only")
        if checkOnly {
            let data: [String: Any] = ["app_exists": FileManager.default.fileExists(atPath: targetApp.path),
                                     "runtime_exists": FileManager.default.fileExists(atPath: targetRuntime.path),
                                     "payload_ready": FileManager.default.isExecutableFile(atPath: payload.appendingPathComponent("python/bin/python3").path),
                                     "codex_available": cli != nil, "mutations": false]
            let json = try! JSONSerialization.data(withJSONObject: data, options: [.sortedKeys])
            print(String(data: json, encoding: .utf8)!)
            NSApp.terminate(nil)
            return
        }
        let preview = CommandLine.arguments.firstIndex(of: "--render-preview")
        NSApp.setActivationPolicy(preview == nil ? .regular : .prohibited)
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 580, height: 560), styleMask: [.titled, .closable, .miniaturizable], backing: .buffered, defer: false)
        window.title = "安装 Bark Bridge"; window.delegate = self; window.center()
        let content = InstallerBackground(); window.contentView = content
        let icon = NSImageView()
        icon.image = NSImage(contentsOf: Bundle.main.resourceURL!.appendingPathComponent("AppIcon.icns"))
        icon.imageScaling = .scaleProportionallyUpOrDown
        icon.translatesAutoresizingMaskIntoConstraints = false
        icon.widthAnchor.constraint(equalToConstant: 64).isActive = true
        icon.heightAnchor.constraint(equalToConstant: 64).isActive = true
        let title = label("从 Mac，连接到你的手机。", size: 25, weight: .semibold)
        let version = Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? ""
        let subtitle = label("Bark Bridge \(version) · Apple Silicon", secondary: true)
        let header = NSStackView(views: [title, subtitle]); header.orientation = .vertical; header.alignment = .leading; header.spacing = 7
        let hero = NSStackView(views: [icon, header]); hero.spacing = 18; hero.alignment = .centerY
        let divider = NSBox(); divider.boxType = .separator
        let intro = label("无需 Xcode，也无需自己安装 Python。安装后，在接收设备面板填写 iPhone Bark 的推送地址，即可发送测试。", size: 14)
        let locations = label("安装位置\n/Applications/Bark.app\n~/Library/Application Support/CodexBark", size: 12, secondary: true)
        attachBox = NSButton(checkboxWithTitle: "同时接入默认 Codex 配置目录（~/.codex）", target: nil, action: nil)
        attachBox.state = cli == nil ? .off : .on; attachBox.isEnabled = cli != nil
        attachBox.font = .systemFont(ofSize: 13)
        let permission = label(cli == nil ? "未找到 Codex CLI：可先安装 App，稍后按安装指南接入。" : "接入会先备份配置，再合并本工具的 Hook 与共享 Skill。操作授权仍由你决定。", size: 12, secondary: true)
        let previewNotice = label("GitHub 分发 · 尚未经过 Apple 公证", size: 13, weight: .semibold)
        previewNotice.textColor = .systemOrange
        let notice = label("尚未使用 Developer ID 签名或 Apple 公证，首次打开可能被 macOS 拦截。请先核对下载来源与校验值，再参考 Apple 的安全打开说明。", size: 12, secondary: true)
        let security = NSButton(title: "Apple 安全打开说明 ↗", target: self, action: #selector(securityInfo)); security.bezelStyle = .inline
        security.font = .systemFont(ofSize: 12)
        status = label("首次安装专用。检测到已有 App 或运行目录时，会停止安装并保留原文件。", size: 12, secondary: true)
        spinner = NSProgressIndicator(); spinner.style = .spinning; spinner.isDisplayedWhenStopped = false; spinner.controlSize = .small
        installButton = NSButton(title: "安装并打开", target: self, action: #selector(install)); installButton.bezelStyle = .rounded; installButton.keyEquivalent = "\r"
        let quit = NSButton(title: "关闭", target: self, action: #selector(close)); quit.bezelStyle = .rounded
        let spacer = NSView()
        let footer = NSStackView(views: [spinner, spacer, quit, installButton]); footer.spacing = 12
        spacer.setContentHuggingPriority(.defaultLow, for: .horizontal)
        let stack = NSStackView(views: [hero, divider, intro, locations, attachBox, permission, previewNotice, notice, security, status, footer])
        stack.orientation = .vertical; stack.alignment = .leading; stack.spacing = 17
        stack.translatesAutoresizingMaskIntoConstraints = false; content.addSubview(stack)
        NSLayoutConstraint.activate([stack.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 32), stack.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -32), stack.topAnchor.constraint(equalTo: content.topAnchor, constant: 30), footer.widthAnchor.constraint(equalTo: stack.widthAnchor)])
        for view: NSView in [divider, intro, locations, permission, notice, status!] { view.widthAnchor.constraint(equalTo: stack.widthAnchor).isActive = true }
        if preview == nil {
            refreshExisting()
            window.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps: true)
        } else if let index = preview, index + 1 < CommandLine.arguments.count {
            window.setFrameOrigin(NSPoint(x: -15000, y: -15000))
            content.layoutSubtreeIfNeeded()
            let bitmap = content.bitmapImageRepForCachingDisplay(in: content.bounds)!
            content.cacheDisplay(in: content.bounds, to: bitmap)
            try? bitmap.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: CommandLine.arguments[index + 1]))
            NSApp.terminate(nil)
        }
    }

    func refreshExisting() {
        let fm = FileManager.default
        // Symlinks (including broken ones) count as occupied destinations.
        let occupied = [targetApp, targetRuntime].contains { (try? fm.attributesOfItem(atPath: $0.path)) != nil }
        if occupied {
            installButton.isEnabled = false; attachBox.isEnabled = false
            let hasApp = fm.fileExists(atPath: targetApp.path)
            status.stringValue = hasApp ? "检测到已有 App，已停止。请使用现有 App；本助手不会覆盖设备、规则或备份。" : "检测到已有运行目录或安装残留，已停止。请按安装指南先核实并备份，勿删除正在使用的数据。"
        }
    }

    @objc func securityInfo() { NSWorkspace.shared.open(URL(string: "https://support.apple.com/102445")!) }
    @objc func close() { if !busy { NSApp.terminate(nil) } }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
    func windowShouldClose(_ sender: NSWindow) -> Bool { !busy }

    @objc func install() {
        if installed { NSWorkspace.shared.openApplication(at: targetApp, configuration: .init()); NSApp.terminate(nil); return }
        refreshExisting(); guard installButton.isEnabled, !busy else { return }
        busy = true; installButton.isEnabled = false; attachBox.isEnabled = false; spinner.startAnimation(nil)
        status.stringValue = "正在复制程序与运行环境，请稍候…"
        let attach = attachBox.state == .on
        DispatchQueue.global(qos: .userInitiated).async {
            let process = Process(); process.executableURL = payload.appendingPathComponent("python/bin/python3")
            var args = ["-I", "-B", payload.appendingPathComponent("install.py").path, "--apply", "--payload", payload.path, "--json-result"]
            if attach { args += ["--codex-home", FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".codex").path] }
            process.arguments = args
            var env = ProcessInfo.processInfo.environment
            for key in Array(env.keys) where key.hasPrefix("PYTHON") { env.removeValue(forKey: key) }
            if let cli = self.cli { env["CODEX_CLI_BIN"] = cli.path }
            process.environment = env
            let out = Pipe(); process.standardOutput = out; process.standardError = FileHandle.nullDevice; process.standardInput = FileHandle.nullDevice
            var ok = false
            do {
                try process.run(); let data = out.fileHandleForReading.readDataToEndOfFile(); process.waitUntilExit()
                let lines = String(data: data, encoding: .utf8)?.split(separator: "\n") ?? []
                if let last = lines.last, let json = last.data(using: .utf8), let result = try? JSONSerialization.jsonObject(with: json) as? [String: Any] {
                    ok = process.terminationStatus == 0 && result["ok"] as? Bool == true
                }
            } catch { }
            DispatchQueue.main.async {
                self.busy = false; self.spinner.stopAnimation(nil); self.installed = ok
                self.installButton.title = ok ? "打开 Bark" : "安装已停止"; self.installButton.isEnabled = ok
                self.status.stringValue = ok ? (attach ? "安装完成，Codex 已接入。请添加手机并确认收到测试；现有 Codex 聊天可能需要重载。" : "安装完成。请添加手机；Codex 接入步骤见下载包中的安装指南。") : "安装未完成。请检查目标目录权限或 Codex CLI；失败目录与配置备份会保留，按安装指南恢复后再试。"
                if ok { NSWorkspace.shared.openApplication(at: targetApp, configuration: .init()) }
            }
        }
    }
}
let app = NSApplication.shared
let delegate = Installer(); app.delegate = delegate
withExtendedLifetime(delegate) { app.run() }
