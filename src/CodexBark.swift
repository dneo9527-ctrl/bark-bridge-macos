import AppKit
import Foundation
import Darwin
import UniformTypeIdentifiers

let support = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support/CodexBark")
let loginFile = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/LaunchAgents/local.codex.bark.menu.plist")
let preferences = UserDefaults(suiteName: "local.codex.bark.menu")!
func shellQuote(_ text: String) -> String { "'" + text.replacingOccurrences(of: "'", with: "'\"'\"'") + "'" }
let pythonURL: URL = {
    let bundled = support.appendingPathComponent("python/bin/python3")
    return FileManager.default.isExecutableFile(atPath: bundled.path) ? bundled : URL(fileURLWithPath: "/usr/bin/python3")
}()
let barkScriptCommand = shellQuote(pythonURL.path) + " " + shellQuote(support.appendingPathComponent("bark.py").path)
let taskWrapperCommand = shellQuote(support.appendingPathComponent("bark-run").path)
var installedAppURL: URL {
    var path = [CChar](repeating: 0, count: 4096)
    let executable: URL
    if proc_pidpath(getpid(), &path, UInt32(path.count)) > 0 {
        executable = URL(fileURLWithPath: String(cString: path))
    } else { executable = Bundle.main.executableURL! }
    var bundle = executable.deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
    if bundle.lastPathComponent == "Codex Bark Panel.app" {
        bundle = bundle.deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
    }
    return bundle.resolvingSymlinksInPath()
}

func backend(_ args: [String], input: Data? = nil, script: String = "bark.py", timeout: Double = 30) -> [String: Any] {
    let p = Process()
    p.executableURL = pythonURL
    p.arguments = [support.appendingPathComponent(script).path] + args + (script == "updater.py" ? ["--app", installedAppURL.path] : [])
    let out = Pipe()
    p.standardOutput = out
    p.standardError = FileHandle.nullDevice
    let incoming = Pipe()
    if input != nil { p.standardInput = incoming } else { p.standardInput = FileHandle.nullDevice }
    do {
        try p.run()
        let deadline = DispatchSource.makeTimerSource(queue: .global(qos: .utility))
        deadline.schedule(deadline: .now() + timeout)
        deadline.setEventHandler { if p.isRunning { kill(p.processIdentifier, SIGKILL) } }
        deadline.resume()
        defer { deadline.cancel() }
        if let input = input {
            incoming.fileHandleForWriting.write(input)
            try? incoming.fileHandleForWriting.close()
        }
        let data = out.fileHandleForReading.readDataToEndOfFile()
        p.waitUntilExit()
        guard p.terminationStatus == 0 else { return ["ok": false, "result": "sender_unavailable"] }
        if let d = try? JSONSerialization.jsonObject(with: data) as? [String: Any] { return d }
        let text = String(data: data, encoding: .utf8) ?? ""
        return ["ok": false,
                "result": text.contains("服务器已接受") ? "accepted" : (text.contains("已暂停") ? "disabled" : "send_failed")]
    } catch { return ["ok": false, "result": "send_failed"] }
}

func memoryMB() -> Int {
    var info = mach_task_basic_info()
    var count = mach_msg_type_number_t(MemoryLayout<mach_task_basic_info>.size / MemoryLayout<integer_t>.size)
    let result = withUnsafeMutablePointer(to: &info) {
        $0.withMemoryRebound(to: integer_t.self, capacity: Int(count)) {
            task_info(mach_task_self_, task_flavor_t(MACH_TASK_BASIC_INFO), $0, &count)
        }
    }
    return result == KERN_SUCCESS ? Int(info.resident_size / 1024 / 1024) : 0
}


func reasonText(_ value: String) -> String {
    ["accepted":"服务器已接收", "partial":"部分设备发送成功", "dns_error":"无法解析服务器", "connect_error":"无法连接服务器", "timeout":"连接或发送超时", "tls_error":"安全连接或证书异常", "transport_error":"网络传输失败", "http_auth":"服务器拒绝认证", "endpoint_missing":"服务器入口不存在", "rate_limited":"服务器请求过多", "server_error":"服务器暂时异常", "request_rejected":"服务器拒绝请求", "api_rejected":"推送服务未接受", "invalid_response":"服务器响应格式异常", "sender_unavailable":"本机发送工具不可用", "unknown_error":"本机发送异常", "local_config_error":"本地配置异常", "invalid_rules":"本地规则异常", "multiple_failures":"多种发送原因，请查看诊断", "disabled":"总推送已暂停", "no_devices_enabled":"没有启用的接收设备", "route_no_targets":"指定设备不可用，已跳过", "quiet_hours":"每日勿扰，已跳过", "not_configured":"尚未添加接收设备", "unknown_device":"设备已不存在", "source_disabled":"来源已暂停", "source_muted":"来源临时静音，已跳过", "event_disabled":"这类提醒已关闭", "unknown_source":"来源已不存在", "deduplicated":"重复提醒已合并"][value] ?? "状态已更新"
}

func reportText(_ data: [String: Any], devices: [[String: Any]]) -> String {
    if let rows = data["device_results"] as? [[String: Any]], !rows.isEmpty {
        return rows.map { row in
            let id = row["device_id"] as? String ?? ""
            let name = devices.first { $0["id"] as? String == id }?["name"] as? String ?? "已移除或未知设备"
            let reason = reasonText(row["reason"] as? String ?? "unknown_error")
            let http = row["http"] as? Int ?? 0
            return name + "：" + reason + (http >= 400 ? "（HTTP \(http)）" : "")
        }.joined(separator: "\n") + "\n\n网关成功不代表手机已收到，请在设备上确认。"
    }
    return reasonText(data["reason"] as? String ?? data["result"] as? String ?? "unknown_error")
}

func eventText(_ row: [String: Any]) -> String {
    let dateString = row["at"] as? String ?? ""
    let parser = ISO8601DateFormatter()
    parser.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    let formatter = DateFormatter()
    formatter.locale = Locale(identifier: "zh_CN")
    formatter.dateFormat = "MM-dd HH:mm"
    let date = parser.date(from: dateString).map { formatter.string(from: $0) } ?? ""
    let kind = ["test": "测试通知", "permission": "等待授权", "question": "等待回答", "complete":"本轮完成", "application": "应用提醒", "source_test": "来源测试", "control": "设置", "battery":"电量提醒"][row["kind"] as? String ?? ""] ?? "通知"
    let result = ["accepted": "服务器已接收", "send_failed": "发送失败", "deduplicated": "已合并",
                  "source_muted": "来源临时静音，已跳过", "quiet_hours": "每日勿扰，已跳过", "route_no_targets": "指定设备不可用，已跳过", "partial": "部分设备已接收", "no_devices_enabled": "接收设备已暂停", "not_configured": "未设置设备", "disabled": "已暂停", "source_disabled": "来源已暂停", "event_disabled":"这类提醒已关闭", "unknown_source": "来源已移除", "enabled": "已开启"][row["status"] as? String ?? ""] ?? "状态已更新"
    let detail = row["status"] as? String == "send_failed" ? reasonText(row["reason"] as? String ?? "unknown_error") : result
    let http = row["http"] as? Int ?? 0
    let total = row["total"] as? Int ?? 0
    let battery = (row["battery_percent"] as? Int).map { " · \($0)%" } ?? ""
    let count = total > 1 ? " · \(row["delivered"] as? Int ?? 0)/\(total) 台" : ""
    return "\(date)  ·  \(kind)\(battery)\n\(detail)\(count)\(http > 0 && http != 200 ? " · HTTP \(http)" : "")"
}

final class NavigationItem: NSView {
    var selected = false { didSet { needsDisplay = true } }
    override func draw(_ dirtyRect: NSRect) {
        guard selected else { return }
        NSColor.controlAccentColor.withAlphaComponent(0.12).setFill()
        NSBezierPath(roundedRect: bounds, xRadius: 6, yRadius: 6).fill()
    }
}

final class NavigationStrip: NSView {
    var selectedSegment = 0 { didSet { refresh() } }
    weak var actionTarget: AnyObject?
    var action: Selector
    var buttons: [NSButton] = []
    var items: [NavigationItem] = []
    var focusButton: NSButton { buttons[selectedSegment] }
    init(labels: [String], symbols: [String], target: AnyObject, action: Selector) {
        self.actionTarget = target; self.action = action
        super.init(frame: .zero)
        setAccessibilityLabel("页面导航")
        for (index, title) in labels.enumerated() {
            let item = NavigationItem()
            let button = NSButton(title: title, target: self, action: #selector(choose(_:)))
            button.tag = index; button.isBordered = false; button.bezelStyle = .inline
            button.font = .systemFont(ofSize: 12, weight: .medium)
            button.image = NSImage(systemSymbolName: symbols[index], accessibilityDescription: nil)?.withSymbolConfiguration(.init(pointSize: 11, weight: .medium))
            button.imagePosition = .imageLeading; button.focusRingType = .exterior
            button.imageHugsTitle = true; button.alignment = .center
            button.setAccessibilityLabel(title); button.identifier = .init(title)
            button.translatesAutoresizingMaskIntoConstraints = false; item.addSubview(button)
            NSLayoutConstraint.activate([
                button.leadingAnchor.constraint(equalTo: item.leadingAnchor),
                button.trailingAnchor.constraint(equalTo: item.trailingAnchor),
                button.topAnchor.constraint(equalTo: item.topAnchor),
                button.bottomAnchor.constraint(equalTo: item.bottomAnchor)
            ])
            buttons.append(button); items.append(item)
        }
        let stack = NSStackView(views: items); stack.orientation = .horizontal
        stack.distribution = .fillEqually; stack.spacing = 3
        stack.translatesAutoresizingMaskIntoConstraints = false; addSubview(stack)
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: leadingAnchor, constant: 3),
            stack.trailingAnchor.constraint(equalTo: trailingAnchor, constant: -3),
            stack.topAnchor.constraint(equalTo: topAnchor, constant: 3),
            stack.bottomAnchor.constraint(equalTo: bottomAnchor, constant: -3)
        ])
        refresh()
    }
    required init?(coder: NSCoder) { fatalError("not used") }
    override func draw(_ dirtyRect: NSRect) {
        NSColor.controlBackgroundColor.setFill()
        NSBezierPath(roundedRect: bounds, xRadius: 8, yRadius: 8).fill()
    }
    func refresh() {
        for (index, button) in buttons.enumerated() {
            items[index].selected = index == selectedSegment
            let color: NSColor = index == selectedSegment ? .controlAccentColor : .secondaryLabelColor
            button.contentTintColor = color
            button.attributedTitle = NSAttributedString(string: button.title, attributes: [.font: NSFont.systemFont(ofSize: 12, weight: index == selectedSegment ? .semibold : .medium), .foregroundColor: color])
            button.setAccessibilityValue(index == selectedSegment ? "已选择" : "")
        }
    }
    @objc func choose(_ sender: NSButton) {
        selectedSegment = sender.tag
        NSApp.sendAction(action, to: actionTarget, from: self)
    }
}

final class SourceSwitch: NSSwitch { var sourceID = "" }
final class SourceMenuButton: NSButton { var sourceID = "" }
final class DeviceSwitch: NSSwitch { var deviceID = "" }
final class DeviceMenuButton: NSButton { var deviceID = "" }
final class HistoryDetailsButton: NSButton { var event: [String: Any] = [:] }

final class AppDelegate: NSObject, NSApplicationDelegate, NSMenuDelegate, NSWindowDelegate, NSTableViewDataSource, NSTableViewDelegate {
    var item: NSStatusItem!
    var window: NSWindow?
    var stateLabel: NSTextField!
    var detailLabel: NSTextField!
    var lastLabel: NSTextField!
    var lastTitleLabel: NSTextField?
    var lastTimeLabel: NSTextField?
    var memoryLabel: NSTextField!
    var toggleButton: NSButton!
    var loginButton: NSButton!
    var testButton: NSButton!
    var lowMemoryButton: NSButton!
    var resourceTimer: Timer?
    var pushSwitch: NSSwitch!
    var pageControl: NavigationStrip!
    var pageSlot: NSView!
    var pageHeight: NSLayoutConstraint?
    var historyTable: NSTableView?
    var historyScrollHeight: NSLayoutConstraint?
    var deviceScroll: NSScrollView?
    var deviceScrollHeight: NSLayoutConstraint?
    var normalModeButton: NSButton!
    var historyText: NSTextView?
    var sourceHint: NSTextField?
    var selectedPage = 0
    var sourceEntries: [[String: Any]] = []
    var selectedSourceID = "codex"
    var sourceTable: NSTableView?
    var deviceWindow: NSWindow?
    var deviceTable: NSTableView?
    var deviceCountLabel: NSTextField?
    var deviceEntries: [[String: Any]] = []
    var selectedDeviceID = ""
    var activeDeviceCount = 0
    var startupSwitch: NSSwitch?
    var residentSwitch: NSSwitch?
    var batterySwitch: NSSwitch?
    var updateSwitch: NSSwitch?
    var updateButton: NSButton?
    var updateLabel: NSTextField?
    var updateState: [String: Any] = [:]
    var updateBusy = false
    let updateQueue = DispatchQueue(label: "local.codex.bark.panel.updates", qos: .utility)
    var modeHint: NSTextField?
    var resumeAt: Double = 0
    var configured = false
    var enabled = false
    var busy = false
    var saving = false
    var loading = false
    var reloadAfterSave = false
    var loadGeneration = 0
    let backendQueue = DispatchQueue(label: "local.codex.bark.panel.backend", qos: .userInitiated)
    var events: [[String: Any]] = []
    var latestEvent: [String: Any]?
    var sourceRoutes: [String: [String]] = [:]
    var quietSettings: [String: Any] = ["enabled":false, "start":"23:00", "end":"08:00"]
    var quietActive = false
    var policyHealthy = true
    var codexEvents: [String: Bool] = ["permission":true, "question":true, "complete":true]
    var codexPreview = true
    var presetName = "custom"
    var presetButton: NSButton?
    var presetChoice: NSPopUpButton?
    var presetDuration: NSPopUpButton?
    var presetPreview: NSTextField?
    var templateChoice: NSPopUpButton?
    var templateDescription: NSTextField?
    var eventChecks: [NSButton] = []
    var healthChecking = false
    var healthButton: NSButton?
    var historyResult = "all"
    var historySource = "all"
    var historyResultPopup: NSPopUpButton?
    var historySourcePopup: NSPopUpButton?
    var historyCountLabel: NSTextField?
    var quietButton: NSButton?
    var routeChecks: [NSButton] = []
    var routeModePopup: NSPopUpButton?
    var quietFormToggle: NSButton?
    var quietStartField: NSTextField?
    var quietEndField: NSTextField?
    let panelOnly = Bundle.main.bundleIdentifier == "local.codex.bark.panel" || ProcessInfo.processInfo.arguments.contains("--panel-only")

    func applicationDidFinishLaunching(_ notification: Notification) {
        if let index = CommandLine.arguments.firstIndex(of: "--render-settings"), index + 1 < CommandLine.arguments.count {
            NSApp.setActivationPolicy(.prohibited)
            selectedPage = 2; configured = true; enabled = true
            makeWindow(); window?.setFrameOrigin(NSPoint(x: -15000, y: -15000))
            if let view = window?.contentView {
                view.layoutSubtreeIfNeeded()
                if let bitmap = view.bitmapImageRepForCachingDisplay(in: view.bounds) {
                    view.cacheDisplay(in: view.bounds, to: bitmap)
                    try? bitmap.representation(using: .png, properties: [:])?.write(to: URL(fileURLWithPath: CommandLine.arguments[index + 1]))
                }
            }
            NSApp.terminate(nil); return
        }
        _ = backend(["app-register", "--app", installedAppURL.path], script: "manage.py")
        NSApp.setActivationPolicy(panelOnly ? .regular : .accessory)
        if let name = Bundle.main.object(forInfoDictionaryKey: "CFBundleIconFile") as? String, let url = Bundle.main.url(forResource: name, withExtension: "icns"), let icon = NSImage(contentsOf: url) { NSApp.applicationIconImage = icon }
        installMainMenu()
        DistributedNotificationCenter.default().addObserver(self, selector: #selector(openPage(_:)), name: .init("CodexBarkOpenPage"), object: nil)
        if let requested = ProcessInfo.processInfo.arguments.first(where: { $0.hasPrefix("--page=") }) {
            selectedPage = ["overview":0, "history":1, "settings":2, "sources":3][String(requested.dropFirst(7))] ?? 0
        }
        DistributedNotificationCenter.default().addObserver(self, selector: #selector(settingsChanged(_:)), name: .init("CodexBarkSettingsChanged"), object: nil)
        if !panelOnly { createStatusItem() }
        if !preferences.bool(forKey: "initialized") {
            do { try setLogin(true) } catch { }
            preferences.set(true, forKey: "initialized")
        }
        if ProcessInfo.processInfo.arguments.contains("--background") { reload() }
        else { showWindow(nil) }
        if !panelOnly { rebuildMenu(item.menu!) }
        if ProcessInfo.processInfo.arguments.contains("--page=devices") { showDevices() }
        refreshUpdates()
    }

    var zeroResident: Bool { preferences.bool(forKey: "zeroResidentMode") }
    var batteryEnabled: Bool { sourceEntries.first { $0["id"] as? String == "mac-battery" }?["enabled"] as? Bool ?? true }
    @objc func settingsChanged(_ note: Notification) {
        if note.object as? String != String(getpid()) { reload() }
    }

    func createStatusItem() {
        item = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        item.autosaveName = "CodexBarkStatus"
        item.isVisible = !zeroResident
        item.button?.setAccessibilityLabel("Codex Bark 手机推送")
        let menu = NSMenu(); menu.delegate = self; item.menu = menu
    }

    func writeMenuStatus() {
        if panelOnly { return }
        let rect = item.button?.window?.frame ?? .zero
        let onScreen = item.isVisible && NSScreen.screens.contains { $0.frame.intersects(rect) && rect.width > 0 }
        let info: [String: Any] = ["pid": getpid(), "zero_resident_mode": zeroResident,
                                    "icon_on_screen": onScreen, "icon_x": rect.origin.x,
                                    "memory_mb": memoryMB(), "panel_open": window?.isVisible ?? false]
        if let data = try? JSONSerialization.data(withJSONObject: info) {
            let url = support.appendingPathComponent("menu-state.json")
            try? data.write(to: url, options: .atomic)
            try? FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: url.path)
        }
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        showWindow(nil)
        return true
    }

    func applicationDidBecomeActive(_ notification: Notification) {
        guard panelOnly else { return }
        refreshUpdates()
        if window != nil { reload() }
        if let device = deviceWindow, device.isVisible {
            device.makeKeyAndOrderFront(nil)
        } else if let panel = window {
            if panel.isMiniaturized { panel.deminiaturize(nil) }
            panel.makeKeyAndOrderFront(nil)
        }
    }

    func installMainMenu() {
        let main = NSMenu()
        let app = NSMenuItem()
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "关于 Bark", action: #selector(showAbout), keyEquivalent: "").target = self
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "隐藏 Bark", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h").target = NSApp
        let hideOthers = appMenu.addItem(withTitle: "隐藏其他", action: #selector(NSApplication.hideOtherApplications(_:)), keyEquivalent: "h")
        hideOthers.target = NSApp; hideOthers.keyEquivalentModifierMask = [.command, .option]
        appMenu.addItem(withTitle: "显示全部", action: #selector(NSApplication.unhideAllApplications(_:)), keyEquivalent: "").target = NSApp
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "退出 Bark 面板", action: #selector(quit), keyEquivalent: "q").target = self
        app.submenu = appMenu
        main.addItem(app)
        let file = NSMenuItem(title: "文件", action: nil, keyEquivalent: "")
        let fileMenu = NSMenu(title: "文件")
        fileMenu.addItem(withTitle: "关闭窗口", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
        file.submenu = fileMenu; main.addItem(file)
        let edit = NSMenuItem(title: "编辑", action: nil, keyEquivalent: "")
        let editMenu = NSMenu(title: "编辑")
        editMenu.addItem(withTitle: "剪切", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        editMenu.addItem(withTitle: "复制", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        editMenu.addItem(withTitle: "粘贴", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        editMenu.addItem(withTitle: "全选", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        edit.submenu = editMenu
        main.addItem(edit)
        let windows = NSMenuItem(title: "窗口", action: nil, keyEquivalent: "")
        let windowsMenu = NSMenu(title: "窗口")
        windowsMenu.addItem(withTitle: "最小化", action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m")
        windowsMenu.addItem(withTitle: "显示主面板", action: #selector(showWindow(_:)), keyEquivalent: "").target = self
        windows.submenu = windowsMenu; main.addItem(windows); NSApp.windowsMenu = windowsMenu
        NSApp.mainMenu = main
    }

    func reload() {
        if saving { reloadAfterSave = true; return }
        loadGeneration += 1
        let generation = loadGeneration, view = selectedPage == 1 ? "history" : "panel"
        loading = true; updateWindowIfPresent()
        backendQueue.async {
            let snapshot = backend(["snapshot", "--view", view])
            DispatchQueue.main.async {
                guard generation == self.loadGeneration, !self.saving else { return }
                self.loading = false
                guard snapshot["ok"] as? Bool == true else {
                    self.updateWindowIfPresent()
                    self.stateLabel?.stringValue = "状态暂不可用"
                    return
                }
                self.applySnapshot(snapshot)
            }
        }
    }

    func mutate(_ args: [String], input: Data? = nil, success: (([String: Any]) -> Void)? = nil) {
        guard !busy else { updateWindowIfPresent(); return }
        saving = true; busy = true; loading = false; loadGeneration += 1
        let view = selectedPage == 1 ? "history" : "panel"
        updateWindowIfPresent(); sourceTable?.reloadData(); deviceTable?.reloadData()
        backendQueue.async {
            let result = backend(args + ["--with-snapshot", "--view", view], input: input)
            DispatchQueue.main.async {
                self.saving = false; self.busy = false
                if result["ok"] as? Bool == true {
                    if let snapshot = result["snapshot"] as? [String: Any], snapshot["ok"] as? Bool == true {
                        self.applySnapshot(snapshot)
                    } else { self.reload() }
                    success?(result)
                    self.announceSettings()
                } else {
                    self.updateWindowIfPresent(); self.sourceTable?.reloadData(); self.deviceTable?.reloadData()
                    self.alert("设置未保存", "本地操作未完成，请刷新状态后重试。")
                    self.reload()
                }
                if self.reloadAfterSave || view != (self.selectedPage == 1 ? "history" : "panel") {
                    self.reloadAfterSave = false; self.reload()
                }
            }
        }
    }

    func applySnapshot(_ snapshot: [String: Any]) {
        let status = snapshot["status"] as? [String: Any] ?? [:]
        configured = status["configured"] as? Bool ?? false
        enabled = status["enabled"] as? Bool ?? false
        resumeAt = status["resume_at"] as? Double ?? 0
        resourceTimer?.invalidate(); resourceTimer = nil
        if resumeAt > Date().timeIntervalSince1970 {
            resourceTimer = Timer.scheduledTimer(withTimeInterval: max(0.1, resumeAt - Date().timeIntervalSince1970), repeats: false) { [weak self] _ in self?.reload() }
        }
        activeDeviceCount = status["active_device_count"] as? Int ?? 0
        deviceEntries = snapshot["devices"] as? [[String: Any]] ?? []
        deviceTable?.reloadData()
        fitDevices()
        deviceCountLabel?.stringValue = deviceEntries.isEmpty ? "尚未添加设备" : "已启用 \(activeDeviceCount) / \(deviceEntries.count) 台"
        events = snapshot["events"] as? [[String: Any]] ?? []
        latestEvent = snapshot["latest_event"] as? [String: Any]
        sourceEntries = snapshot["sources"] as? [[String: Any]] ?? []
        let policy = snapshot["policy"] as? [String: Any] ?? [:]
        policyHealthy = policy["ok"] as? Bool ?? false
        codexEvents = policy["codex_events"] as? [String: Bool] ?? ["permission":true, "question":true, "complete":true]
        codexPreview = policy["codex_preview"] as? Bool ?? true
        presetName = policy["preset"] as? String ?? "custom"
        sourceRoutes = policy["routes"] as? [String: [String]] ?? [:]
        quietSettings = policy["quiet_hours"] as? [String: Any] ?? ["enabled":false, "start":"23:00", "end":"08:00"]
        quietActive = policy["quiet_now"] as? Bool ?? false
        let symbol = !configured ? "bell.badge" : (enabled ? "bell.badge.fill" : "bell.slash")
        if let item = item {
            item.button?.image = NSImage(systemSymbolName: symbol, accessibilityDescription: "Codex Bark")
            item.button?.image?.isTemplate = true
            item.button?.title = ""
            item.button?.toolTip = "Codex Bark · \(configured ? (enabled ? "推送已开启" : "推送已暂停") : "尚未配置")"
        }
        if window != nil { updateWindow() }
        sourceTable?.reloadData()
        writeMenuStatus()
    }

    func menuNeedsUpdate(_ menu: NSMenu) { reload(); rebuildMenu(menu) }

    func add(_ menu: NSMenu, _ title: String, _ action: Selector?, checked: Bool? = nil) -> NSMenuItem {
        let row = NSMenuItem(title: title, action: action, keyEquivalent: "")
        row.target = self
        if let checked = checked { row.state = checked ? .on : .off }
        menu.addItem(row)
        return row
    }

    func rebuildMenu(_ menu: NSMenu) {
        menu.removeAllItems()
        _ = add(menu, "Codex Bark · \(configured ? (enabled ? "已开启" : "已暂停") : "尚未配置")", nil)
        _ = add(menu, "开启 iPhone 推送", #selector(toggle), checked: enabled)
        menu.addItem(.separator())
        _ = add(menu, "打开控制面板…", #selector(showWindow(_:)))
        let test = add(menu, busy ? "正在发送测试…" : "发送测试通知", #selector(sendTest))
        test.isEnabled = configured && enabled && !busy
        _ = add(menu, "最近推送记录…", #selector(showHistory))
        _ = add(menu, "设置或更换手机…", #selector(configurePhone))
        _ = add(menu, "找回菜单栏图标…", #selector(restoreIcon))
        menu.addItem(.separator())
        _ = add(menu, "登录 Mac 时显示菜单栏", #selector(toggleLogin), checked: FileManager.default.fileExists(atPath: loginFile.path))
        _ = add(menu, "隐藏菜单栏", #selector(toggleLowMemory), checked: zeroResident)
        _ = add(menu, "本界面内存约 \(memoryMB()) MB · 无网络轮询", nil)
        _ = add(menu, "退出界面后，Codex 推送仍会继续", nil)
        menu.addItem(.separator())
        _ = add(menu, "卸载本机推送…", #selector(uninstall))
        _ = add(menu, "退出菜单栏界面", #selector(quit))
    }

    func label(_ text: String, size: CGFloat = 13, weight: NSFont.Weight = .regular, color: NSColor = .labelColor) -> NSTextField {
        let field = NSTextField(wrappingLabelWithString: text)
        field.font = .systemFont(ofSize: size, weight: weight)
        field.textColor = color
        field.preferredMaxLayoutWidth = 384
        field.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        return field
    }

    @objc func showWindow(_ sender: Any?) {
        reload()
        if window == nil { makeWindow() }
        NSApp.activate(ignoringOtherApps: true)
        if window?.isMiniaturized == true { window?.deminiaturize(nil) }
        window?.makeKeyAndOrderFront(nil)
    }

    func row(_ views: [NSView], spacing: CGFloat = 10) -> NSStackView {
        let stack = NSStackView(views: views); stack.orientation = .horizontal
        stack.alignment = .centerY; stack.spacing = spacing
        return stack
    }

    func column(_ views: [NSView], spacing: CGFloat = 8) -> NSStackView {
        let stack = NSStackView(views: views); stack.orientation = .vertical
        stack.alignment = .leading; stack.spacing = spacing
        return stack
    }

    func spacer() -> NSView {
        let view = NSView(); view.setContentHuggingPriority(.defaultLow, for: .horizontal)
        return view
    }

    func button(_ title: String, _ action: Selector, link: Bool = false) -> NSButton {
        let button = NSButton(title: title, target: self, action: action)
        button.bezelStyle = .rounded; button.controlSize = .small; button.focusRingType = .exterior
        button.font = .systemFont(ofSize: 12, weight: link ? .medium : .regular)
        if link { button.isBordered = false; button.contentTintColor = .controlAccentColor }
        return button
    }

    func card(_ contents: NSView, padding: CGFloat = 12) -> NSBox {
        let box = NSBox(); box.boxType = .custom; box.cornerRadius = 10
        box.borderWidth = 0.5; box.borderColor = .separatorColor.withAlphaComponent(0.18)
        box.fillColor = .controlBackgroundColor
        box.translatesAutoresizingMaskIntoConstraints = false
        contents.translatesAutoresizingMaskIntoConstraints = false
        box.addSubview(contents)
        NSLayoutConstraint.activate([
            contents.leadingAnchor.constraint(equalTo: box.leadingAnchor, constant: padding),
            contents.trailingAnchor.constraint(equalTo: box.trailingAnchor, constant: -padding),
            contents.topAnchor.constraint(equalTo: box.topAnchor, constant: padding),
            contents.bottomAnchor.constraint(equalTo: box.bottomAnchor, constant: -padding)
        ])
        return box
    }

    func makeWindow() {
        let w = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 440, height: 304),
                         styleMask: [.titled, .closable, .miniaturizable, .fullSizeContentView], backing: .buffered, defer: false)
        w.title = "Bark"; w.titleVisibility = .hidden; w.titlebarAppearsTransparent = true
        w.isReleasedWhenClosed = false; w.delegate = self; w.center(); window = w
        let backdrop = NSVisualEffectView(frame: w.contentView!.bounds)
        backdrop.material = .windowBackground; backdrop.blendingMode = .behindWindow
        backdrop.state = .active; backdrop.autoresizingMask = [.width, .height]; w.contentView = backdrop
        let icon = NSImageView(image: NSImage(systemSymbolName: "bell.fill", accessibilityDescription: nil)!)
        icon.contentTintColor = .secondaryLabelColor; icon.translatesAutoresizingMaskIntoConstraints = false
        icon.widthAnchor.constraint(equalToConstant: 18).isActive = true
        icon.heightAnchor.constraint(equalToConstant: 18).isActive = true
        stateLabel = label("", size: 11, weight: .medium)
        pushSwitch = NSSwitch(); pushSwitch.target = self; pushSwitch.action = #selector(toggle)
        pushSwitch.controlSize = .small; pushSwitch.setAccessibilityLabel("推送开关")
        let header = row([icon, label("Bark", size: 16, weight: .semibold), spacer(), stateLabel, pushSwitch], spacing: 12)
        pageControl = NavigationStrip(labels: ["概览", "来源", "记录", "设置"], symbols: ["square.grid.2x2", "square.stack", "clock", "slider.horizontal.3"], target: self, action: #selector(selectPage(_:)))
        pageControl.selectedSegment = [0: 0, 3: 1, 1: 2, 2: 3][selectedPage] ?? 0
        pageSlot = NSView(); pageSlot.translatesAutoresizingMaskIntoConstraints = false
        pageHeight = pageSlot.heightAnchor.constraint(equalToConstant: 192)
        pageHeight?.isActive = true
        let root = column([header, pageControl, pageSlot], spacing: 10)
        root.translatesAutoresizingMaskIntoConstraints = false; backdrop.addSubview(root)
        NSLayoutConstraint.activate([
            root.leadingAnchor.constraint(equalTo: backdrop.leadingAnchor, constant: 16),
            root.trailingAnchor.constraint(equalTo: backdrop.trailingAnchor, constant: -16),
            root.topAnchor.constraint(equalTo: backdrop.topAnchor, constant: 36),
            header.heightAnchor.constraint(equalToConstant: 26),
            pageControl.heightAnchor.constraint(equalToConstant: 32),
            header.widthAnchor.constraint(equalTo: root.widthAnchor),
            pageControl.widthAnchor.constraint(equalTo: root.widthAnchor),
            pageSlot.widthAnchor.constraint(equalTo: root.widthAnchor)
        ])
        buildPage(); updateWindow()
    }

    @objc func selectPage(_ sender: NavigationStrip) {
        selectedPage = [0, 3, 1, 2][sender.selectedSegment]
        reload(); buildPage(); updateWindow()
        if selectedPage == 2 { refreshUpdates() }
    }

    func buildPage() {
        for view in pageSlot.subviews { view.removeFromSuperview() }
        detailLabel = nil; lastLabel = nil; lastTitleLabel = nil; lastTimeLabel = nil; memoryLabel = nil; loginButton = nil
        testButton = nil; lowMemoryButton = nil; normalModeButton = nil; historyText = nil; sourceHint = nil
        sourceTable = nil; historyTable = nil; historyScrollHeight = nil; presetButton = nil
        historyResultPopup = nil; historySourcePopup = nil; historyCountLabel = nil; quietButton = nil; healthButton = nil
        startupSwitch = nil; residentSwitch = nil; batterySwitch = nil; modeHint = nil
        updateSwitch = nil; updateButton = nil; updateLabel = nil
        let content: NSView
        var bodyHeight: CGFloat = 192
        switch selectedPage {
        case 1:
            let table = NSTableView(); historyTable = table
            table.rowHeight = 48; table.intercellSpacing = .zero; table.headerView = nil
            table.delegate = self; table.dataSource = self; table.style = .plain
            table.selectionHighlightStyle = .none; table.backgroundColor = .clear; table.focusRingType = .default
            table.columnAutoresizingStyle = .lastColumnOnlyAutoresizingStyle
            table.setAccessibilityLabel("最近推送记录")
            let col = NSTableColumn(identifier: .init("history")); col.width = 392; table.addTableColumn(col)
            let scroll = NSScrollView(); scroll.hasVerticalScroller = true; scroll.autohidesScrollers = true
            scroll.borderType = .noBorder; scroll.drawsBackground = false; scroll.documentView = table
            let listHeight = CGFloat(max(2, min(4, events.count)) * 48)
            historyScrollHeight = scroll.heightAnchor.constraint(equalToConstant: listHeight)
            historyScrollHeight?.isActive = true
            let top = row([label("最近记录", size: 12, color: .secondaryLabelColor), spacer(), button("刷新", #selector(refresh), link: true)])
            let group = card(scroll, padding: 8)
            historyResultPopup = NSPopUpButton(frame: .zero, pullsDown: false)
            historyResultPopup!.controlSize = .small; historyResultPopup!.font = .systemFont(ofSize: 12)
            for (title, id) in [("全部结果","all"),("成功","success"),("失败","failed"),("跳过","skipped")] {
                historyResultPopup!.addItem(withTitle: title); historyResultPopup!.lastItem?.representedObject = id
            }
            historyResultPopup!.selectItem(at: ["all","success","failed","skipped"].firstIndex(of: historyResult) ?? 0)
            historyResultPopup!.target = self; historyResultPopup!.action = #selector(changeHistoryFilter)
            historyResultPopup!.setAccessibilityLabel("记录结果筛选")
            historySourcePopup = NSPopUpButton(frame: .zero, pullsDown: false)
            historySourcePopup!.controlSize = .small; historySourcePopup!.font = .systemFont(ofSize: 12)
            historySourcePopup!.target = self; historySourcePopup!.action = #selector(changeHistoryFilter)
            historySourcePopup!.setAccessibilityLabel("记录来源筛选")
            historySourcePopup!.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
            let filters = row([historyResultPopup!, historySourcePopup!], spacing: 8)
            historyResultPopup!.widthAnchor.constraint(equalToConstant: 120).isActive = true
            historySourcePopup!.setContentHuggingPriority(.defaultLow, for: .horizontal)
            historyCountLabel = label("", size: 11, color: .secondaryLabelColor)
            content = column([top, filters, group, historyCountLabel!], spacing: 10)
            filters.widthAnchor.constraint(equalTo: content.widthAnchor).isActive = true
            group.widthAnchor.constraint(equalTo: content.widthAnchor).isActive = true
            top.widthAnchor.constraint(equalTo: content.widthAnchor).isActive = true
            bodyHeight = listHeight + 34
        case 2:
            startupSwitch = NSSwitch(); startupSwitch!.controlSize = .small
            startupSwitch!.target = self; startupSwitch!.action = #selector(toggleLogin)
            startupSwitch!.setAccessibilityLabel("登录时启动")
            residentSwitch = NSSwitch(); residentSwitch!.controlSize = .small
            residentSwitch!.target = self; residentSwitch!.action = #selector(changeResident)
            residentSwitch!.setAccessibilityLabel("显示菜单栏")
            residentSwitch!.toolTip = "隐藏图标后，电池提醒仍会保留轻量监听。"
            batterySwitch = NSSwitch(); batterySwitch!.controlSize = .small
            batterySwitch!.target = self; batterySwitch!.action = #selector(toggleBattery)
            batterySwitch!.setAccessibilityLabel("低电量提醒")
            batterySwitch!.toolTip = "未接电时，20% 和 10% 各尝试一次；接电或回升至 25% 以上复位。遵守总开关、来源路由和勿扰。"
            let batteryRow = row([label("低电量提醒", size: 13), spacer(), batterySwitch!])
            let startup = row([label("登录时启动", size: 13), spacer(), startupSwitch!])
            let resident = row([label("显示菜单栏", size: 13), spacer(), residentSwitch!])
            quietButton = button("未开启", #selector(editQuietHours), link: true)
            quietButton!.setAccessibilityLabel("设置每日勿扰时段")
            let quietRow = row([label("每日勿扰", size: 13), spacer(), quietButton!])
            let settings = column([batteryRow, rule(), quietRow, rule(), startup, rule(), resident], spacing: 11)
            for view in settings.arrangedSubviews { view.widthAnchor.constraint(equalTo: settings.widthAnchor).isActive = true }
            let box = card(settings)
            modeHint = label("", size: 11, color: .secondaryLabelColor)
            healthButton = button("接入自检", #selector(checkHealth), link: true)
            healthButton!.setAccessibilityLabel("接入自检")
            let footer = row([healthButton!, button("诊断报告", #selector(createSupportReport), link: true), spacer(), button("关于", #selector(showAbout), link: true), button("高级…", #selector(showAdvanced(_:)), link: true)], spacing: 12)
            let version = Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? ""
            let build = Bundle.main.object(forInfoDictionaryKey: "CFBundleVersion") as? String ?? ""
            updateButton = button("检查更新", #selector(updateAction))
            let updateTitle = row([label("软件更新", size: 13, weight: .medium), spacer(), updateButton!])
            let versionRow = label("当前版本 \(version) · Build \(build)", size: 12, color: .secondaryLabelColor)
            updateLabel = label("可从 GitHub 检查新版本", size: 11, color: .secondaryLabelColor)
            updateSwitch = NSSwitch(); updateSwitch!.controlSize = .small
            updateSwitch!.target = self; updateSwitch!.action = #selector(toggleAutoUpdate)
            updateSwitch!.setAccessibilityLabel("自动下载更新")
            let autoRow = row([label("自动下载更新", size: 13), spacer(), updateSwitch!])
            let hint = label("每 12 小时检查，下载后由你确认重启安装。", size: 11, color: .secondaryLabelColor)
            let links = row([button("GitHub ↗", #selector(openRepository), link: true), button("更新日志 ↗", #selector(openReleases), link: true), spacer()], spacing: 12)
            let updates = column([updateTitle, versionRow, updateLabel!, rule(), autoRow, hint, links], spacing: 9)
            for view in updates.arrangedSubviews { view.widthAnchor.constraint(equalTo: updates.widthAnchor).isActive = true }
            let updateBox = card(updates)
            content = column([box, updateBox, footer], spacing: 12)
            bodyHeight = 365
            box.widthAnchor.constraint(equalTo: content.widthAnchor).isActive = true
            updateBox.widthAnchor.constraint(equalTo: content.widthAnchor).isActive = true
            footer.widthAnchor.constraint(equalTo: content.widthAnchor).isActive = true
        case 3:
            let add = button("添加应用", #selector(addApplications), link: true)
            add.image = NSImage(systemSymbolName: "plus", accessibilityDescription: nil)
            add.imagePosition = .imageLeading
            let top = row([label("通知来源", size: 13, weight: .medium), spacer(), button("内置模板", #selector(showTemplates), link: true), add])
            let table = NSTableView()
            table.rowHeight = 52; table.delegate = self; table.dataSource = self
            table.headerView = nil; table.intercellSpacing = .zero; table.backgroundColor = .clear
            table.style = .plain; table.selectionHighlightStyle = .none; table.focusRingType = .default
            table.usesAlternatingRowBackgroundColors = false
            table.columnAutoresizingStyle = .lastColumnOnlyAutoresizingStyle
            table.setAccessibilityLabel("通知来源")
            let col = NSTableColumn(identifier: .init("source")); col.width = 408
            table.addTableColumn(col); sourceTable = table
            let scroll = NSScrollView()
            scroll.hasVerticalScroller = sourceEntries.count > 3; scroll.autohidesScrollers = true
            scroll.borderType = .noBorder; scroll.drawsBackground = false; scroll.documentView = table
            let group = NSBox()
            group.boxType = .custom; group.cornerRadius = 10; group.borderWidth = 0.5
            group.borderColor = .separatorColor.withAlphaComponent(0.3)
            group.fillColor = .controlBackgroundColor
            group.translatesAutoresizingMaskIntoConstraints = false
            scroll.translatesAutoresizingMaskIntoConstraints = false; group.addSubview(scroll)
            NSLayoutConstraint.activate([
                group.heightAnchor.constraint(equalToConstant: CGFloat(max(1, min(3, sourceEntries.count)) * 52)),
                scroll.leadingAnchor.constraint(equalTo: group.leadingAnchor),
                scroll.trailingAnchor.constraint(equalTo: group.trailingAnchor),
                scroll.topAnchor.constraint(equalTo: group.topAnchor),
                scroll.bottomAnchor.constraint(equalTo: group.bottomAnchor)
            ])
            let help = button("接入说明", #selector(sourceHelp), link: true)
            help.image = NSImage(systemSymbolName: "info.circle", accessibilityDescription: nil)
            help.imagePosition = .imageLeading
            let footer = row([label("\(sourceEntries.count) 个来源", size: 11, color: .secondaryLabelColor), spacer(), help])
            content = column([top, group, footer], spacing: 10)
            bodyHeight = CGFloat(max(1, min(3, sourceEntries.count)) * 52 + 64)
            for view in [top, group, footer] { view.widthAnchor.constraint(equalTo: content.widthAnchor).isActive = true }
        default:
            let phone = NSImageView(image: NSImage(systemSymbolName: "iphone", accessibilityDescription: nil)!)
            phone.contentTintColor = .secondaryLabelColor; phone.translatesAutoresizingMaskIntoConstraints = false
            phone.widthAnchor.constraint(equalToConstant: 20).isActive = true
            phone.heightAnchor.constraint(equalToConstant: 26).isActive = true
            detailLabel = label("", size: 11, color: .secondaryLabelColor)
            let device = row([phone, column([label("接收设备", size: 13, weight: .medium), detailLabel], spacing: 4), spacer(), button("管理", #selector(showDevices), link: true)], spacing: 10)
            let historyTop = row([label("最近通知", size: 11, color: .secondaryLabelColor), spacer(), button("记录", #selector(showHistory), link: true)])
            lastTitleLabel = label("", size: 13, weight: .medium)
            lastTimeLabel = label("", size: 11, color: .secondaryLabelColor)
            lastTimeLabel?.font = .monospacedDigitSystemFont(ofSize: 11, weight: .regular)
            lastTimeLabel?.setContentCompressionResistancePriority(.required, for: .horizontal)
            let eventTitle = row([lastTitleLabel!, spacer(), lastTimeLabel!])
            lastLabel = label("", size: 11, color: .secondaryLabelColor)
            let recent = column([historyTop, eventTitle, lastLabel], spacing: 4)
            let divider = rule()
            testButton = button("发送测试", #selector(sendTest))
            testButton.image = NSImage(systemSymbolName: "paperplane", accessibilityDescription: nil)
            testButton.imagePosition = .imageLeading
            let actions = row([spacer(), testButton])
            presetButton = button("", #selector(showPresets), link: true)
            presetButton!.setAccessibilityLabel("选择提醒场景预设")
            let presetRow = row([label("提醒场景", size: 13), spacer(), presetButton!])
            let summary = column([device, rule(), presetRow, divider, recent, actions], spacing: 10)
            let group = card(summary)
            for view in [device, presetRow, divider, recent, historyTop, eventTitle, actions] { view.widthAnchor.constraint(equalTo: group.widthAnchor, constant: -24).isActive = true }
            content = group
        }
        content.translatesAutoresizingMaskIntoConstraints = false; pageSlot.addSubview(content)
        NSLayoutConstraint.activate([content.leadingAnchor.constraint(equalTo: pageSlot.leadingAnchor),
                                     content.trailingAnchor.constraint(equalTo: pageSlot.trailingAnchor),
                                     content.topAnchor.constraint(equalTo: pageSlot.topAnchor)])
        updateWindow()
        window?.contentView?.layoutSubtreeIfNeeded()
        bodyHeight = ceil(content.fittingSize.height)
        pageHeight?.constant = bodyHeight
        fitWindow(bodyHeight: bodyHeight)
        window?.initialFirstResponder = pageControl.focusButton
        window?.recalculateKeyViewLoop()
    }

    func fitWindow(bodyHeight: CGFloat) {
        guard let window = window else { return }
        var frame = NSRect(x: 0, y: 0, width: 440, height: bodyHeight + 128)
        frame.origin = NSPoint(x: window.frame.minX, y: window.frame.maxY - frame.height)
        if let screen = window.screen ?? NSScreen.main {
            let visible = screen.visibleFrame.insetBy(dx: 8, dy: 8)
            frame.origin.x = max(visible.minX, min(frame.origin.x, visible.maxX - frame.width))
            frame.origin.y = max(visible.minY, min(frame.origin.y, visible.maxY - frame.height))
        }
        if abs(window.frame.height - frame.height) > 1 || abs(window.frame.width - frame.width) > 1 {
            window.setFrame(frame, display: true, animate: window.isVisible)
        }
    }

    func rule() -> NSBox {
        let box = NSBox(); box.boxType = .separator
        return box
    }

    func updateWindow() {
        updateSoftwareControls()
        var pausedText = "已暂停"
        if resumeAt > Date().timeIntervalSince1970 {
            let date = DateFormatter(); date.dateFormat = "HH:mm"
            pausedText = "暂停至 " + date.string(from: Date(timeIntervalSince1970: resumeAt))
        }
        stateLabel?.stringValue = !configured ? "待设置" : (enabled ? (!policyHealthy ? "规则异常" : (quietActive ? "勿扰中" : "已开启")) : pausedText)
        stateLabel?.textColor = !configured || (enabled && (quietActive || !policyHealthy)) ? .systemOrange : (enabled ? .systemGreen : .secondaryLabelColor)
        pushSwitch?.state = enabled ? .on : .off; pushSwitch?.isEnabled = configured && !busy && !loading
        detailLabel?.stringValue = !configured ? "添加手机，开始接收提醒" : (deviceEntries.count == 1 ? "\(deviceEntries[0]["name"] as? String ?? "iPhone") · \(activeDeviceCount > 0 ? "已启用" : "已暂停")" : "\(activeDeviceCount) 台接收 · \(deviceEntries.count) 台已保存")
        testButton?.isEnabled = configured && enabled && activeDeviceCount > 0 && !busy
        testButton?.title = busy ? (saving ? "保存中…" : (healthChecking ? "检查中…" : "处理中…")) : "发送测试"
        if saving { stateLabel?.stringValue = "保存中…"; stateLabel?.textColor = .secondaryLabelColor }
        startupSwitch?.isEnabled = !busy
        batterySwitch?.isEnabled = !busy
        residentSwitch?.isEnabled = !busy
        quietButton?.isEnabled = !busy
        healthButton?.title = healthChecking ? "检查中…" : "接入自检"
        healthButton?.isEnabled = !busy
        if healthChecking { stateLabel?.stringValue = "自检中…"; stateLabel?.textColor = .secondaryLabelColor }
        startupSwitch?.state = FileManager.default.fileExists(atPath: loginFile.path) ? .on : .off
        startupSwitch?.isEnabled = !busy && (!zeroResident || batteryEnabled)
        batterySwitch?.state = batteryEnabled ? .on : .off
        residentSwitch?.state = zeroResident ? .off : .on
        modeHint?.stringValue = zeroResident ? (batteryEnabled ? "菜单栏已隐藏，保留电池监听。" : "界面按需打开，推送继续工作。") : "设置关闭后，仅保留菜单栏与电池监听。"
        let qEnabled = quietSettings["enabled"] as? Bool ?? false
        quietButton?.title = qEnabled ? "\(quietSettings["start"] as? String ?? "23:00")–\(quietSettings["end"] as? String ?? "08:00")" : "未开启"
        let lines = latestEvent.map(eventText)?.components(separatedBy: "\n") ?? []
        let heading = lines.first?.components(separatedBy: "  ·  ") ?? []
        lastTitleLabel?.stringValue = heading.last ?? "暂无通知"
        lastTimeLabel?.stringValue = heading.count > 1 ? heading[0] : ""
        lastLabel?.stringValue = lines.count > 1 ? lines[1] : "通知记录会显示在这里"
        presetButton?.title = ["need":"需要我时", "away":"离开电脑", "focus":"专注片刻", "custom":"自定义"][presetName] ?? "自定义"
        presetButton?.isEnabled = !busy && !loading && policyHealthy
        populateHistorySources()
        historyTable?.reloadData()
        historyCountLabel?.stringValue = "\(filteredHistory.count) 条 · 最多最近 200 条"
        if selectedPage == 1, let height = historyScrollHeight {
            height.constant = CGFloat(max(2, min(4, filteredHistory.count)) * 48)
            window?.contentView?.layoutSubtreeIfNeeded()
            if let content = pageSlot?.subviews.first {
                let size = ceil(content.fittingSize.height)
                pageHeight?.constant = size; fitWindow(bodyHeight: size)
            }
        }
        updateSourceControls()
    }

    @objc func changeResident() { toggleLowMemory(); buildPage(); updateWindow() }
    func refreshUpdates(_ args: [String] = ["status"]) {
        guard !updateBusy else { return }
        updateBusy = true; updateSoftwareControls()
        updateQueue.async {
            let result = backend(args, script: "updater.py", timeout: 240)
            DispatchQueue.main.async {
                self.updateBusy = false
                if result["ok"] as? Bool == true { self.updateState = result }
                else { self.updateState["error"] = result["error"] as? String ?? "update_failed" }
                self.updateSoftwareControls()
                if args.first == "automatic" {
                    DistributedNotificationCenter.default().postNotificationName(.init("CodexBarkUpdatePreferenceChanged"), object: nil, userInfo: nil, deliverImmediately: true)
                }
                if args.first == "start", result["started"] as? Bool == true { NSApp.terminate(nil) }
            }
        }
    }
    func updateSoftwareControls() {
        updateSwitch?.state = updateState["automatic"] as? Bool == true ? .on : .off
        updateSwitch?.isEnabled = !updateBusy
        let status = updateState["status"] as? String ?? ""
        updateButton?.title = updateBusy ? "处理中…" : (status == "ready" ? "重启并更新" : (status == "available" ? "下载更新" : "检查更新"))
        updateButton?.isEnabled = !updateBusy
        if updateBusy { updateLabel?.stringValue = "正在处理更新，请稍候…"; return }
        if let error = updateState["error"] as? String {
            let texts = ["signature_invalid":"发布签名未通过，已停止更新", "checksum_mismatch":"下载校验未通过，请重试", "rate_limited":"GitHub 请求受限，请稍后重试", "busy":"更新任务正在运行，请稍后检查", "update_rolled_back":"更新未完成，已恢复原版本", "recovery_required":"上次更新待恢复，请重新打开 Bark", "install_permission":"应用目录不可写，请检查权限"]
            updateLabel?.stringValue = texts[error] ?? "更新未完成，请检查网络后重试"
            return
        }
        let tag = updateState["tag"] as? String ?? ""
        switch status {
        case "ready": updateLabel?.stringValue = "\(tag) 已下载，发布签名与文件校验通过"
        case "available": updateLabel?.stringValue = "发现新版本 \(tag)"
        case "current": updateLabel?.stringValue = "当前已是最新可用版本"
        case "no_compatible_release": updateLabel?.stringValue = "暂未发布适用的更新包"
        default: updateLabel?.stringValue = "可从 GitHub 检查新版本"
        }
    }
    @objc func toggleAutoUpdate() {
        refreshUpdates(["automatic", updateSwitch?.state == .on ? "on" : "off"])
    }
    @objc func updateAction() {
        switch updateState["status"] as? String ?? "" {
        case "ready": refreshUpdates(["start", "--parent-pid", String(getpid())])
        case "available": refreshUpdates(["download"])
        default: refreshUpdates(["check"])
        }
    }
    @objc func openRepository() { NSWorkspace.shared.open(URL(string: "https://github.com/dneo9527-ctrl/bark-bridge-macos")!) }
    @objc func openReleases() { NSWorkspace.shared.open(URL(string: "https://github.com/dneo9527-ctrl/bark-bridge-macos/releases")!) }
    @objc func openSettings() { navigate(to: 2) }
    func navigate(to page: Int) {
        selectedPage = page; showWindow(nil)
        pageControl.selectedSegment = [0: 0, 3: 1, 1: 2, 2: 3][page] ?? 0
        buildPage(); updateWindow()
    }
    @objc func openPage(_ notification: Notification) {
        let page = notification.userInfo?["page"] as? String ?? "overview"
        navigate(to: ["overview":0, "history":1, "settings":2, "sources":3][page] ?? 0)
        if page == "devices" { showDevices() }
    }
    var enabledEventNames: [String] {
        [("permission", "授权"), ("question", "提问"), ("complete", "完成")].filter { codexEvents[$0.0] ?? true }.map { $0.1 }
    }

    @objc func editCodexEvents() {
        guard let parent = window, !busy, policyHealthy else { return }
        let dialog = NSAlert(); dialog.messageText = "Codex 提醒类型"
        dialog.informativeText = "分别选择要接收的手机提醒。总开关、来源开关和勿扰仍然生效；操作授权流程不受影响。"
        let form = NSView(frame: NSRect(x: 0, y: 0, width: 340, height: 108))
        eventChecks = []
        for (index, item) in [("permission", "授权提醒"), ("question", "提问提醒"), ("complete", "本轮完成提醒")].enumerated() {
            let check = NSButton(checkboxWithTitle: item.1, target: nil, action: nil)
            check.frame = NSRect(x: 0, y: CGFloat(76 - index * 32), width: 340, height: 24)
            check.identifier = .init(item.0); check.state = codexEvents[item.0] ?? true ? .on : .off
            check.setAccessibilityLabel(item.1); form.addSubview(check); eventChecks.append(check)
        }
        dialog.accessoryView = form; dialog.addButton(withTitle: "保存"); dialog.addButton(withTitle: "取消")
        dialog.beginSheetModal(for: parent) { response in
            defer { self.eventChecks = [] }
            guard response == .alertFirstButtonReturn else { return }
            var values: [String: Bool] = [:]
            for check in self.eventChecks { if let key = check.identifier?.rawValue { values[key] = check.state == .on } }
            guard let data = try? JSONSerialization.data(withJSONObject: values) else { return }
            self.mutate(["events-set"], input: data)
        }
    }

    @objc func toggleCodexPreview() {
        guard !busy, policyHealthy else { return }
        mutate(["preview-set", codexPreview ? "off" : "on"])
    }

    @objc func checkHealth() {
        guard !busy else { return }
        busy = true; healthChecking = true; updateWindowIfPresent()
        DispatchQueue.global(qos: .utility).async {
            let report = backend(["health"], script: "manage.py")
            DispatchQueue.main.async {
                self.busy = false; self.healthChecking = false; self.updateWindowIfPresent()
                self.showHealthReport(report)
            }
        }
    }

    func showHealthReport(_ report: [String: Any]) {
        guard let parent = window else { return }
        guard report["ok"] as? Bool == true, let checks = report["checks"] as? [[String: Any]], !checks.isEmpty else {
            alert("暂未完成自检", "自检组件暂不可用，请稍后重试。当前推送设置没有更改。")
            return
        }
        let dialog = NSAlert()
        dialog.messageText = ["ready":"本地接入检查通过", "attention":"有需要处理的项目", "unknown":"部分项目暂无法确认"][report["summary"] as? String ?? ""] ?? "接入自检结果"
        dialog.informativeText = "仅核验本机接入，不更改配置或发送测试。检查通过不代表所有聊天场景都能触发，也不等于手机已收到。"
        let rows = Array(checks.prefix(100))
        let scroll = NSScrollView(frame: NSRect(x: 0, y: 0, width: 360, height: CGFloat(min(6, rows.count) * 48)))
        scroll.hasVerticalScroller = rows.count > 6; scroll.autohidesScrollers = true; scroll.drawsBackground = false
        let document = NSView(frame: NSRect(x: 0, y: 0, width: 340, height: CGFloat(rows.count * 48)))
        for (index, check) in rows.enumerated() {
            let status = check["status"] as? String ?? "unknown"
            let y = CGFloat((rows.count - index - 1) * 48)
            let symbol = ["pass":"checkmark.circle.fill", "warning":"exclamationmark.triangle.fill", "off":"minus.circle", "unknown":"questionmark.circle"][status] ?? "questionmark.circle"
            let icon = NSImageView(image: NSImage(systemSymbolName: symbol, accessibilityDescription: ["pass":"正常", "warning":"待处理", "off":"未开启", "unknown":"待确认"][status] ?? "待确认")!)
            icon.contentTintColor = status == "pass" ? .systemGreen : (status == "warning" ? .systemOrange : .secondaryLabelColor)
            icon.frame = NSRect(x: 0, y: y + 18, width: 17, height: 17); document.addSubview(icon)
            let title = NSTextField(labelWithString: String((check["title"] as? String ?? "检查项").prefix(80)))
            title.font = .systemFont(ofSize: 12, weight: .medium); title.lineBreakMode = .byTruncatingTail
            title.frame = NSRect(x: 26, y: y + 26, width: 310, height: 17); document.addSubview(title)
            let detail = NSTextField(labelWithString: String((check["detail"] as? String ?? "暂无法确认").prefix(160)))
            detail.font = .systemFont(ofSize: 11); detail.textColor = .secondaryLabelColor; detail.lineBreakMode = .byTruncatingTail
            detail.toolTip = detail.stringValue; detail.frame = NSRect(x: 26, y: y + 6, width: 310, height: 17); document.addSubview(detail)
        }
        scroll.documentView = document; dialog.accessoryView = scroll; dialog.addButton(withTitle: "好")
        NSApp.activate(ignoringOtherApps: true); dialog.beginSheetModal(for: parent)
    }

    @objc func sourceHelp() {
        if selectedSourceID == "mac-battery" {
            alert("低电量提醒", "未接电源时，20% 与 10% 每档尝试一次。接电或回升到 25% 以上复位。\n遵守总开关、来源路由与每日勿扰；期间跳过或发送失败不补发。\n完全退出 Bark、Mac 睡眠、关机或断网时无法保证提醒。")
            return
        }
        alert("接入说明", "选择软件后，将接入命令配置到它支持的事件入口。测试只验证手机连接。\nCodex 的普通文字提问及部分云端场景可能遗漏。")
    }
    @objc func createSupportReport() {
        guard !busy else { return }
        busy = true; healthChecking = true; updateWindowIfPresent()
        backendQueue.async {
            let result = backend(["support-report"], script: "manage.py")
            DispatchQueue.main.async {
                self.busy = false; self.healthChecking = false; self.updateWindowIfPresent()
                guard result["ok"] as? Bool == true, let report = result["report"] as? [String: Any],
                      let data = try? JSONSerialization.data(withJSONObject: report, options: [.prettyPrinted, .sortedKeys]),
                      let text = String(data: data, encoding: .utf8), let parent = self.window else {
                    self.alert("报告暂不可用", "本地诊断未完成，请稍后重试。"); return
                }
                let dialog = NSAlert(); dialog.messageText = "脱敏诊断报告"
                dialog.informativeText = "只含版本、接入状态和固定错误类别。不会发送通知或修改配置；服务器接收与手机实收需分别确认。"
                let scroll = NSScrollView(frame: NSRect(x: 0, y: 0, width: 360, height: 220))
                scroll.hasVerticalScroller = true; scroll.borderType = .bezelBorder
                let view = NSTextView(frame: scroll.bounds)
                view.isEditable = false; view.isSelectable = true; view.font = .monospacedSystemFont(ofSize: 11, weight: .regular)
                view.string = text; view.textContainer?.widthTracksTextView = true
                view.autoresizingMask = [.width]; scroll.documentView = view; dialog.accessoryView = scroll
                dialog.addButton(withTitle: "完成"); dialog.addButton(withTitle: "复制"); dialog.addButton(withTitle: "保存…")
                dialog.beginSheetModal(for: parent) { response in
                    if response == .alertSecondButtonReturn {
                        NSPasteboard.general.clearContents(); NSPasteboard.general.setString(text, forType: .string)
                    } else if response == .alertThirdButtonReturn {
                        let save = NSSavePanel(); save.nameFieldStringValue = "Bark 诊断报告.json"
                        save.allowedContentTypes = [.json]
                        save.beginSheetModal(for: parent) { selected in
                            guard selected == .OK, let url = save.url else { return }
                            do { try data.write(to: url, options: .atomic)
                                try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: url.path)
                            } catch { self.alert("报告未保存", "无法写入所选位置，请换个位置重试。") }
                        }
                    }
                }
            }
        }
    }

    @objc func showPresets() {
        guard !busy, policyHealthy, let parent = window else { return }
        let dialog = NSAlert(); dialog.messageText = "提醒场景"
        dialog.informativeText = "只调整 Codex 提醒。总开关、来源停用、接收设备、路由与勿扰继续生效。"
        let form = NSView(frame: NSRect(x: 0, y: 0, width: 340, height: 134))
        let choice = NSPopUpButton(frame: NSRect(x: 0, y: 100, width: 340, height: 26))
        for (title, key) in [("需要我时", "need"), ("离开电脑", "away"), ("专注片刻", "focus")] {
            choice.addItem(withTitle: title); choice.lastItem?.representedObject = key
        }
        choice.selectItem(at: ["need", "away", "focus"].firstIndex(of: presetName) ?? 0)
        choice.target = self; choice.action = #selector(presetSelectionChanged)
        choice.setAccessibilityLabel("场景预设")
        let duration = NSPopUpButton(frame: NSRect(x: 0, y: 67, width: 160, height: 26))
        for (title, seconds) in [("静音 15 分钟", 900), ("静音 1 小时", 3600)] {
            duration.addItem(withTitle: title); duration.lastItem?.representedObject = seconds
        }
        duration.setAccessibilityLabel("专注静音时长")
        let preview = label("", size: 12, color: .secondaryLabelColor)
        preview.frame = NSRect(x: 0, y: 0, width: 340, height: 60)
        presetChoice = choice; presetDuration = duration; presetPreview = preview
        for v in [choice, duration, preview] { form.addSubview(v) }
        presetSelectionChanged(); dialog.accessoryView = form
        dialog.addButton(withTitle: "应用预设"); dialog.addButton(withTitle: "取消")
        dialog.beginSheetModal(for: parent) { response in
            defer { self.presetChoice = nil; self.presetDuration = nil; self.presetPreview = nil }
            guard response == .alertFirstButtonReturn, let key = choice.selectedItem?.representedObject as? String else { return }
            let seconds = duration.selectedItem?.representedObject as? Int ?? 900
            self.mutate(["preset-set", "--preset", key, "--seconds", String(seconds)])
        }
    }

    @objc func presetSelectionChanged() {
        let mode = presetChoice?.selectedItem?.representedObject as? String ?? "need"
        presetDuration?.isHidden = mode != "focus"
        presetPreview?.stringValue = [
            "need": "授权、提问：开启\n本轮完成：关闭\n解除 Codex 临时静音，其他来源沿用原设置。",
            "away": "授权、提问、本轮完成：开启\n解除 Codex 临时静音。\n不会自动检测离席，也不解除总暂停或勿扰。",
            "focus": "仅临时静音 Codex，期间跳过且不补发。\n电池和其他来源沿用原设置。\n到期不误开启已永久停用的来源。"
        ][mode] ?? ""
    }

    @objc func showTemplates() {
        guard !busy else { return }
        busy = true; updateWindowIfPresent()
        backendQueue.async {
            let status = backend(["status"], script: "claude_bark.py")
            DispatchQueue.main.async {
                self.busy = false; self.updateWindowIfPresent(); self.presentTemplates(status)
            }
        }
    }

    func presentTemplates(_ claudeStatus: [String: Any]) {
        guard let parent = window else { return }
        let dialog = NSAlert(); dialog.messageText = "内置接入模板"
        dialog.informativeText = "Codex 与 Mac 低电量已内置。其他模板通过明确事件触发；选中应用不会自动读取全部系统通知。"
        let form = NSView(frame: NSRect(x: 0, y: 0, width: 350, height: 154))
        let choice = NSPopUpButton(frame: NSRect(x: 0, y: 122, width: 350, height: 26))
        for (title, key) in [("终端长任务结束", "terminal"), ("快捷指令", "shortcuts"), ("Claude Code", "claude"), ("Antigravity App", "antigravity")] {
            choice.addItem(withTitle: title); choice.lastItem?.representedObject = key
        }
        choice.setAccessibilityLabel("内置接入模板类型")
        choice.target = self; choice.action = #selector(templateSelectionChanged)
        let summary = label("", size: 12, color: .secondaryLabelColor)
        summary.frame = NSRect(x: 0, y: 0, width: 350, height: 112)
        templateChoice = choice; templateDescription = summary
        if selectedSourceID == "com.google.antigravity" { choice.selectItem(at: 3) }
        choice.identifier = .init(claudeStatus["configured"] as? Bool == true ? "connected" : (claudeStatus["available"] as? Bool == true ? "available" : "missing"))
        templateSelectionChanged(); form.addSubview(choice); form.addSubview(summary)
        dialog.accessoryView = form; dialog.addButton(withTitle: "使用模板"); dialog.addButton(withTitle: "取消")
        dialog.beginSheetModal(for: parent) { response in
            defer { self.templateChoice = nil; self.templateDescription = nil }
            guard response == .alertFirstButtonReturn, let key = choice.selectedItem?.representedObject as? String else { return }
            if key == "claude" { self.connectClaude() }
            else if key == "antigravity" { self.connectAntigravity() }
            else {
                self.mutate(["template-enable", "--template", key]) { _ in
                    let command = key == "terminal" ? taskWrapperCommand + " /usr/bin/true" : barkScriptCommand + " template-notify --template shortcuts"
                    NSPasteboard.general.clearContents(); NSPasteboard.general.setString(command, forType: .string)
                    self.buildPage()
                    self.alert("模板命令已复制", key == "terminal" ? "将示例里的 /usr/bin/true 换成要运行的程序和参数。保留原退出码；只推送成功或失败，不发送命令与输出。\n管道需显式放在 shell -c 中；终端强制关闭或 Mac 睡眠时无法保证提醒。" : "在 Mac 快捷指令末尾添加“运行 Shell 脚本”，粘贴命令。只有工作流运行到该动作时才发送；前面异常中止不会到达。\n这是显式调用模板，未自动创建或修改你的快捷指令。")
                }
            }
        }
    }

    @objc func templateSelectionChanged() {
        let key = templateChoice?.selectedItem?.representedObject as? String ?? "terminal"
        if key == "claude" {
            let status = templateChoice?.identifier?.rawValue
            let headline = status == "connected" ? "已配置本机提醒入口。" : (status == "available" ? "检测到 Claude Code，可合并接入。" : "未检测到 Claude Code CLI，待安装后接入。")
            templateDescription?.stringValue = headline + "\n接入授权、AskUserQuestion 与本轮结束事件。\n合并已有设置，修改前备份；不改授权策略。\n不读取聊天记录或账号凭据。重新打开会话后生效。"
        } else if key == "antigravity" {
            templateDescription?.stringValue = "接入原生 Antigravity.app 的官方 Hook。\n支持 ask_question、新增资源权限申请与正常结束。\n普通操作审批没有独立事件，不保证覆盖。\n合并全局 Hook 并备份，不改变授权策略。"
        } else if key == "shortcuts" {
            templateDescription?.stringValue = "适用于 Mac 上的快捷指令工作流。\n启用来源后复制“运行 Shell 脚本”动作命令。\n把动作放在工作流末尾，运行到这里才推送。\n与其他应用共用手机、路由、静音及勿扰规则。"
        } else {
            templateDescription?.stringValue = "编译、测试、脚本等长任务结束后通知。\n复制包装命令，替换示例程序即可使用。\n保留输出和退出码，不记录或发送原命令。\n只包装明确指定的任务，不更改你的 Shell 配置。"
        }
    }

    func connectClaude() {
        guard !busy else { return }
        busy = true; updateWindowIfPresent()
        backendQueue.async {
            let result = backend(["attach"], script: "claude_bark.py")
            DispatchQueue.main.async {
                self.busy = false; self.updateWindowIfPresent()
                if result["ok"] as? Bool == true {
                    self.mutate(["template-enable", "--template", "claude"]) { _ in
                        self.buildPage(); self.alert("Claude Code 提醒已接入", "请重新打开 Claude Code 会话。授权、AskUserQuestion 与本轮结束通过本机入口发送；本机设置成功不等于真实事件或手机投递已验证。")
                    }
                } else {
                    self.alert("Claude Code 尚未接入", result["reason"] as? String == "cli_missing" ? "本机未检测到 Claude Code CLI。请从官方渠道安装后，回到内置模板接入；不需要另填手机地址。" : "本地版本或设置暂不可用。原有配置未覆盖，请检查后重试。")
                }
            }
        }
    }

    func connectAntigravity() {
        guard !busy else { return }
        guard let app = NSWorkspace.shared.urlForApplication(withBundleIdentifier: "com.google.antigravity") else {
            alert("未找到 Antigravity App", "请安装并打开一次 Antigravity.app，再使用这个模板。"); return
        }
        busy = true; updateWindowIfPresent()
        backendQueue.async {
            let result = backend(["antigravity-attach", "--app", app.path], script: "manage.py")
            DispatchQueue.main.async {
                self.busy = false; self.updateWindowIfPresent()
                guard result["ok"] as? Bool == true else {
                    self.alert("Antigravity 尚未接入", "Hook 合并未完成，原有配置已保留。请检查本机应用版本或 Hook 配置后重试。"); return
                }
                let input = try? JSONSerialization.data(withJSONObject: ["paths": [app.path]])
                self.mutate(["source-add"], input: input) { _ in
                    self.selectedSourceID = "com.google.antigravity"; self.buildPage()
                    self.alert("Antigravity App 已接入", "请重新打开 Antigravity 会话，让官方 Hook 生效。\n支持工具提问、新增资源权限申请与正常结束；普通操作审批及直接文字提问不保证捕获。\n手机、静音和勿扰沿用现有设置，原本停用的来源保持停用。")
                }
            }
        }
    }

    @objc func showAbout() {
        let version = Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? ""
        alert("Bark " + version, memoryDescription() + "\n右键铃铛切换推送；退出界面后仍按需推送。")
    }
    @objc func showAdvanced(_ sender: NSButton) {
        let menu = NSMenu()
        let restore = NSMenuItem(title: "重新显示图标", action: #selector(restoreIcon), keyEquivalent: "")
        restore.target = self; menu.addItem(restore); menu.addItem(.separator())
        let remove = NSMenuItem(title: "卸载…", action: #selector(uninstall), keyEquivalent: "")
        remove.target = self; menu.addItem(remove)
        DispatchQueue.main.async { menu.popUp(positioning: nil, at: NSPoint(x: 0, y: sender.bounds.maxY), in: sender) }
    }

    @objc func chooseMode(_ sender: NSButton) {
        if (sender.tag == 1) != zeroResident { toggleLowMemory() }
        buildPage(); updateWindow()
    }

    @objc func copyIntegration() {
        guard let source = selectedSource(), let id = source["id"] as? String else { return }
        let command: String
        if id == "terminal-task" { command = taskWrapperCommand + " /usr/bin/true" }
        else if id == "claude-code" || id == "com.google.antigravity" { showTemplates(); return }
        else if id == "shortcuts" { command = barkScriptCommand + " template-notify --template shortcuts" }
        else { command = barkScriptCommand + " notify --source-id " + shellQuote(id) }
        NSPasteboard.general.clearContents(); NSPasteboard.general.setString(command, forType: .string)
        alert("接入命令已复制", "将命令填入该软件的事件入口即可。")
    }

    func selectedSource() -> [String: Any]? {
        sourceEntries.first { $0["id"] as? String == selectedSourceID } ?? sourceEntries.first
    }

    func updateSourceControls() { sourceTable?.reloadData() }

    func numberOfRows(in tableView: NSTableView) -> Int {
        if tableView === deviceTable { return deviceEntries.count }
        if tableView === historyTable { return max(1, filteredHistory.count) }
        return sourceEntries.count
    }
    func tableView(_ tableView: NSTableView, shouldSelectRow row: Int) -> Bool { false }

    func tableView(_ tableView: NSTableView, viewFor tableColumn: NSTableColumn?, row index: Int) -> NSView? {
        if tableView === deviceTable { return deviceCell(index) }
        if tableView === historyTable { return historyCell(index) }
        guard sourceEntries.indices.contains(index) else { return nil }
        let source = sourceEntries[index]
        let name = source["name"] as? String ?? "应用"
        let id = source["id"] as? String ?? ""
        let builtin = source["type"] as? String == "builtin"
        let observed = source["status"] as? String == "observed"
        let cell = NSTableCellView(); cell.backgroundStyle = .normal
        let image: NSImage
        if id == "com.google.antigravity", let url = NSWorkspace.shared.urlForApplication(withBundleIdentifier: id) {
            image = NSWorkspace.shared.icon(forFile: url.path)
        } else if ["terminal-task", "shortcuts", "claude-code", "agy-console"].contains(id) {
            image = NSImage(systemSymbolName: ["terminal-task":"terminal", "shortcuts":"square.stack.3d.up", "claude-code":"bubble.left.and.bubble.right", "agy-console":"terminal"][id]!, accessibilityDescription: nil)!
        } else if id == "mac-battery" {
            image = NSImage(systemSymbolName: "battery.25percent", accessibilityDescription: nil)!
        } else if let path = source["path"] as? String {
            image = NSWorkspace.shared.icon(forFile: path)
        } else if id == "codex", let url = NSWorkspace.shared.urlForApplication(withBundleIdentifier: "com.openai.codex") {
            image = NSWorkspace.shared.icon(forFile: url.path)
        } else {
            image = NSImage(systemSymbolName: "app", accessibilityDescription: nil)!
        }
        let icon = NSImageView(image: image); icon.imageScaling = .scaleProportionallyUpOrDown
        icon.translatesAutoresizingMaskIntoConstraints = false
        let title = NSTextField(labelWithString: name)
        title.font = .systemFont(ofSize: 13, weight: .medium); title.lineBreakMode = .byTruncatingTail
        let subtitleText: String
        let mutedUntil = source["mute_until"] as? Double ?? 0
        if source["enabled"] as? Bool != true {
            subtitleText = "已停用"
        } else if mutedUntil > Date().timeIntervalSince1970 {
            let date = DateFormatter(); date.dateFormat = "HH:mm"
            subtitleText = "静音至 " + date.string(from: Date(timeIntervalSince1970: mutedUntil))
        } else if id == "codex" && enabledEventNames.isEmpty {
            subtitleText = "提醒类型已关闭"
        } else if let targets = sourceRoutes[id] {
            let active = deviceEntries.filter { targets.contains($0["id"] as? String ?? "") && $0["enabled"] as? Bool == true }.count
            subtitleText = active == 0 ? "指定设备不可用" : "\(builtin || observed ? "已接入" : "待接入") · 指定 \(targets.count) 台"
        } else { subtitleText = builtin ? (id == "mac-battery" ? "未接电 · 20% 与 10%" : "已接入 · " + enabledEventNames.joined(separator: "、")) : (observed ? "已接入 · 已收到事件" : "待接入") }
        let subtitle = NSTextField(labelWithString: subtitleText)
        subtitle.font = .systemFont(ofSize: 11); subtitle.textColor = id == "codex" && enabledEventNames.isEmpty ? .systemOrange : (builtin || observed ? .secondaryLabelColor : .systemOrange)
        subtitle.lineBreakMode = .byTruncatingTail
        subtitle.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        let labels = column([title, subtitle], spacing: 4)
        title.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        labels.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        let toggle = SourceSwitch(); toggle.sourceID = id; toggle.controlSize = .small
        toggle.target = self; toggle.action = #selector(toggleSource(_:))
        toggle.state = source["enabled"] as? Bool == true ? .on : .off; toggle.isEnabled = !busy
        toggle.setAccessibilityLabel(name + "允许推送")
        let more = SourceMenuButton(image: NSImage(systemSymbolName: "ellipsis", accessibilityDescription: "更多操作")!, target: self, action: #selector(sourceMenu(_:)))
        more.sourceID = id; more.bezelStyle = .inline; more.isBordered = false
        more.contentTintColor = .secondaryLabelColor; more.toolTip = name + "选项"
        more.setAccessibilityLabel(name + "选项")
        more.widthAnchor.constraint(equalToConstant: 24).isActive = true
        let contents = row([icon, labels, spacer(), toggle, more], spacing: 12)
        contents.translatesAutoresizingMaskIntoConstraints = false; cell.addSubview(contents)
        NSLayoutConstraint.activate([
            contents.leadingAnchor.constraint(equalTo: cell.leadingAnchor, constant: 14),
            contents.trailingAnchor.constraint(equalTo: cell.trailingAnchor, constant: -10),
            contents.centerYAnchor.constraint(equalTo: cell.centerYAnchor),
            icon.widthAnchor.constraint(equalToConstant: 28), icon.heightAnchor.constraint(equalToConstant: 28)
        ])
        if index > 0 {
            let divider = rule(); divider.translatesAutoresizingMaskIntoConstraints = false; cell.addSubview(divider)
            NSLayoutConstraint.activate([
                divider.leadingAnchor.constraint(equalTo: cell.leadingAnchor, constant: 58),
                divider.trailingAnchor.constraint(equalTo: cell.trailingAnchor),
                divider.topAnchor.constraint(equalTo: cell.topAnchor)
            ])
        }
        return cell
    }

    @objc func sourceMenu(_ sender: SourceMenuButton) {
        selectedSourceID = sender.sourceID
        guard let source = selectedSource() else { return }
        let menu = NSMenu()
        let test = NSMenuItem(title: "发送测试", action: #selector(testSource), keyEquivalent: "")
        test.target = self; test.isEnabled = configured && enabled && source["enabled"] as? Bool == true && !busy
        menu.addItem(test)
        if source["id"] as? String == "codex" {
            let types = NSMenuItem(title: "提醒类型…", action: #selector(editCodexEvents), keyEquivalent: "")
            types.target = self; types.isEnabled = !busy && policyHealthy; menu.addItem(types)
            let preview = NSMenuItem(title: "手机内容预览", action: #selector(toggleCodexPreview), keyEquivalent: "")
            preview.target = self; preview.state = codexPreview ? .on : .off
            preview.isEnabled = !busy && policyHealthy; menu.addItem(preview)
            let health = NSMenuItem(title: "接入自检…", action: #selector(checkHealth), keyEquivalent: "")
            health.target = self; health.isEnabled = !busy; menu.addItem(health)
        }
        menu.addItem(.separator())
        let mute = NSMenuItem(title: "临时静音", action: nil, keyEquivalent: "")
        let muteMenu = NSMenu(); muteMenu.autoenablesItems = false
        for (title, seconds) in [("15 分钟", 900), ("1 小时", 3600), ("结束临时静音", 0)] {
            let option = NSMenuItem(title: title, action: #selector(muteSource(_:)), keyEquivalent: "")
            option.target = self; option.representedObject = ["id": sender.sourceID, "seconds": seconds] as [String: Any]
            option.isEnabled = !busy && policyHealthy && (seconds == 0 || source["enabled"] as? Bool == true)
            muteMenu.addItem(option)
        }
        mute.submenu = muteMenu; menu.addItem(mute)
        let route = NSMenuItem(title: "指定接收设备…", action: #selector(editSourceRoute), keyEquivalent: "")
        route.target = self; route.isEnabled = !busy; menu.addItem(route)
        if source["type"] as? String != "builtin" {
            let copy = NSMenuItem(title: "复制接入命令", action: #selector(copyIntegration), keyEquivalent: "")
            copy.target = self; menu.addItem(copy)
            menu.addItem(.separator())
            let remove = NSMenuItem(title: "移除来源…", action: #selector(removeSource), keyEquivalent: "")
            remove.target = self; menu.addItem(remove)
        } else {
            let info = NSMenuItem(title: "接入说明", action: #selector(sourceHelp), keyEquivalent: "")
            info.target = self; menu.addItem(info)
        }
        menu.autoenablesItems = false
        DispatchQueue.main.async {
            menu.popUp(positioning: nil, at: NSPoint(x: sender.bounds.maxX, y: sender.bounds.maxY), in: sender)
        }
    }

    @objc func muteSource(_ sender: NSMenuItem) {
        guard let data = sender.representedObject as? [String: Any], let id = data["id"] as? String, let seconds = data["seconds"] as? Int else { return }
        mutate(["source-mute", "--source-id", id, "--seconds", String(seconds)])
    }

    @objc func addApplications() {
        let picker = NSOpenPanel()
        picker.title = "添加软件"; picker.prompt = "添加"
        picker.message = "选择软件，支持多选。"
        picker.allowedContentTypes = [.applicationBundle]
        picker.canChooseDirectories = false; picker.canChooseFiles = true
        picker.treatsFilePackagesAsDirectories = false; picker.allowsMultipleSelection = true
        picker.directoryURL = URL(fileURLWithPath: "/Applications")
        guard let window = window else { return }
        picker.beginSheetModal(for: window) { response in
            guard response == .OK else { return }
            let paths = picker.urls.map(\.path)
            guard let input = try? JSONSerialization.data(withJSONObject: ["paths": paths]) else { return }
            self.mutate(["source-add"], input: input) { result in
                self.selectedSourceID = (result["added"] as? [String])?.last ?? self.selectedSourceID
                self.buildPage(); self.updateWindow()
            }
        }
    }

    @objc func toggleSource(_ sender: SourceSwitch) {
        let id = sender.sourceID
        mutate(["source-toggle", "--source-id", id, "--source-enabled", sender.state == .on ? "on" : "off"]) { _ in
            if id == "mac-battery" { self.syncBatteryHelper() }
        }
    }

    @objc func toggleBattery(_ sender: NSSwitch) {
        mutate(["source-toggle", "--source-id", "mac-battery", "--source-enabled", sender.state == .on ? "on" : "off"]) { _ in self.syncBatteryHelper() }
    }

    func syncBatteryHelper() {
        if zeroResident {
            if !batteryEnabled { try? setLogin(false) }
            else if preferences.bool(forKey: "loginBeforeZero") { try? setLogin(true) }
        }
        preferences.synchronize()
        DistributedNotificationCenter.default().postNotificationName(.init("CodexBarkModeChanged"), object: nil, userInfo: nil, deliverImmediately: true)
        if batteryEnabled { startMenu(restore: false) }
        updateWindowIfPresent()
    }

    @objc func removeSource() {
        guard let source = selectedSource(), source["type"] as? String != "builtin", let id = source["id"] as? String else { return }
        let confirm = NSAlert(); confirm.messageText = "移除这个通知来源？"
        confirm.informativeText = "仅移除推送配置，不会删除软件；以后可以重新添加。"
        confirm.addButton(withTitle: "取消"); confirm.addButton(withTitle: "移除")
        guard let window = window else { return }
        confirm.beginSheetModal(for: window) { response in
            guard response == .alertSecondButtonReturn else { return }
            self.mutate(["source-remove", "--source-id", id]) { _ in self.selectedSourceID = "codex"; self.buildPage() }
        }
    }

    @objc func testSource() {
        guard let source = selectedSource(), let id = source["id"] as? String, !busy else { return }
        busy = true; updateWindow()
        DispatchQueue.global(qos: .utility).async {
            let result = backend(["source-test", "--source-id", id])
            DispatchQueue.main.async {
                self.busy = false; self.reload()
                self.alert(result["ok"] as? Bool == true ? "测试通知已发送" : "测试未成功",
                           reportText(result, devices: self.deviceEntries) + "\n来源测试仅验证发送链路。")
            }
        }
    }

    func nameForSource(_ id: String) -> String {
        if id == "unknown" { return "未知来源" }
        if id == "system" { return "手动与设置" }
        return sourceEntries.first { $0["id"] as? String == id }?["name"] as? String ?? "已移除来源"
    }

    func category(_ event: [String: Any]) -> String {
        if let value = event["category"] as? String { return value }
        switch event["status"] as? String ?? "" {
        case "accepted": return "success"
        case "partial", "send_failed", "not_configured": return "failed"
        case "disabled", "quiet_hours", "route_no_targets", "no_devices_enabled", "source_disabled", "event_disabled", "unknown_source", "deduplicated": return "skipped"
        default: return "info"
        }
    }

    var filteredHistory: [[String: Any]] {
        Array(events.reversed()).filter {
            (historyResult == "all" || category($0) == historyResult) &&
            (historySource == "all" || ($0["source_id"] as? String ?? "unknown") == historySource)
        }
    }

    func populateHistorySources() {
        guard let popup = historySourcePopup else { return }
        let ids = Set(sourceEntries.compactMap { $0["id"] as? String } + events.map { $0["source_id"] as? String ?? "unknown" } + ["unknown", "system"])
        popup.removeAllItems(); popup.addItem(withTitle: "全部来源"); popup.lastItem?.representedObject = "all"
        for id in ids.sorted() {
            popup.addItem(withTitle: nameForSource(id)); popup.lastItem?.representedObject = id
        }
        popup.selectItem(at: popup.itemArray.firstIndex { $0.representedObject as? String == historySource } ?? 0)
        if popup.indexOfSelectedItem == 0 { historySource = "all" }
    }

    @objc func changeHistoryFilter() {
        historyResult = historyResultPopup?.selectedItem?.representedObject as? String ?? "all"
        historySource = historySourcePopup?.selectedItem?.representedObject as? String ?? "all"
        historyTable?.reloadData()
        historyCountLabel?.stringValue = "\(filteredHistory.count) 条 · 最多最近 200 条"
        if selectedPage == 1, let height = historyScrollHeight {
            height.constant = CGFloat(max(2, min(4, filteredHistory.count)) * 48)
            window?.contentView?.layoutSubtreeIfNeeded()
            if let content = pageSlot?.subviews.first {
                let size = ceil(content.fittingSize.height)
                pageHeight?.constant = size; fitWindow(bodyHeight: size)
            }
        }
    }

    func historyDetail(_ text: String) -> NSTextField {
        let field = NSTextField(labelWithString: text)
        field.font = .systemFont(ofSize: 11); field.textColor = .secondaryLabelColor
        field.lineBreakMode = .byTruncatingTail
        field.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        return field
    }

    @objc func routeModeChanged(_ sender: NSPopUpButton) {
        for check in routeChecks { check.isEnabled = sender.indexOfSelectedItem == 1 }
    }

    @objc func editSourceRoute() {
        guard let source = selectedSource(), let id = source["id"] as? String, let parent = window else { return }
        let dialog = NSAlert(); dialog.messageText = "接收设备 · " + (source["name"] as? String ?? "应用")
        dialog.informativeText = "指定设备不可用时跳过，不会改投其他手机。"
        let visible = max(1, min(5, deviceEntries.count))
        let listHeight = CGFloat(visible * 26)
        let form = NSView(frame: NSRect(x: 0, y: 0, width: 340, height: listHeight + 42))
        let mode = NSPopUpButton(frame: NSRect(x: 0, y: listHeight + 14, width: 340, height: 26), pullsDown: false)
        mode.addItems(withTitles: ["跟随全局已启用设备", "仅发送给下方勾选设备"])
        mode.selectItem(at: sourceRoutes[id] == nil ? 0 : 1)
        mode.target = self; mode.action = #selector(routeModeChanged(_:)); mode.setAccessibilityLabel("接收设备范围")
        routeModePopup = mode; form.addSubview(mode)
        let scroll = NSScrollView(frame: NSRect(x: 0, y: 0, width: 340, height: listHeight))
        scroll.hasVerticalScroller = deviceEntries.count > 5; scroll.autohidesScrollers = true
        scroll.drawsBackground = false; scroll.borderType = .noBorder
        let document = NSView(frame: NSRect(x: 0, y: 0, width: 324, height: CGFloat(max(1, deviceEntries.count) * 26)))
        routeChecks = []
        for (index, device) in deviceEntries.enumerated() {
            let deviceID = device["id"] as? String ?? ""
            let active = device["enabled"] as? Bool ?? true
            let title = (device["name"] as? String ?? "设备") + (active ? "" : "（已停用）")
            let check = NSButton(checkboxWithTitle: title, target: nil, action: nil)
            check.frame = NSRect(x: 0, y: CGFloat((deviceEntries.count - index - 1) * 26), width: 324, height: 24)
            check.identifier = .init(deviceID); check.cell?.lineBreakMode = .byTruncatingTail
            check.state = (sourceRoutes[id]?.contains(deviceID) ?? active) ? .on : .off
            check.isEnabled = mode.indexOfSelectedItem == 1
            check.setAccessibilityLabel(title + "作为接收设备")
            document.addSubview(check); routeChecks.append(check)
        }
        if deviceEntries.isEmpty {
            let empty = label("先在接收设备中添加手机", size: 12, color: .secondaryLabelColor)
            empty.frame = NSRect(x: 0, y: 2, width: 324, height: 20); document.addSubview(empty)
        }
        scroll.documentView = document; form.addSubview(scroll); dialog.accessoryView = form
        dialog.addButton(withTitle: "保存"); dialog.addButton(withTitle: "取消")
        dialog.beginSheetModal(for: parent) { response in
            defer { self.routeChecks = []; self.routeModePopup = nil }
            guard response == .alertFirstButtonReturn else { return }
            let ids = self.routeChecks.filter { $0.state == .on }.compactMap { $0.identifier?.rawValue }
            guard let data = try? JSONSerialization.data(withJSONObject: ["mode": mode.indexOfSelectedItem == 0 ? "all" : "selected", "device_ids": ids]) else { return }
            self.mutate(["route-set", "--source-id", id], input: data)
        }
    }

    @objc func quietFormChanged() {
        let enabled = quietFormToggle?.state == .on
        quietStartField?.isEnabled = enabled; quietEndField?.isEnabled = enabled
    }

    @objc func editQuietHours() {
        guard let parent = window else { return }
        let dialog = NSAlert(); dialog.messageText = "每日勿扰"
        dialog.informativeText = "自动提醒在时段内跳过，结束后不补发。手动测试仍遵守总开关。"
        let form = NSView(frame: NSRect(x: 0, y: 0, width: 340, height: 112))
        let toggle = NSButton(checkboxWithTitle: "开启每日勿扰", target: self, action: #selector(quietFormChanged))
        toggle.frame = NSRect(x: 0, y: 82, width: 340, height: 24)
        toggle.state = quietSettings["enabled"] as? Bool == true ? .on : .off
        quietFormToggle = toggle; form.addSubview(toggle)
        let startLabel = label("开始", size: 12); startLabel.frame = NSRect(x: 0, y: 43, width: 38, height: 20)
        let endLabel = label("结束", size: 12); endLabel.frame = NSRect(x: 178, y: 43, width: 38, height: 20)
        let start = NSTextField(frame: NSRect(x: 42, y: 40, width: 100, height: 24))
        let end = NSTextField(frame: NSRect(x: 220, y: 40, width: 100, height: 24))
        start.stringValue = quietSettings["start"] as? String ?? "23:00"
        end.stringValue = quietSettings["end"] as? String ?? "08:00"
        for field in [start, end] { field.font = .monospacedDigitSystemFont(ofSize: 12, weight: .regular); field.placeholderString = "HH:mm" }
        start.setAccessibilityLabel("勿扰开始时间，HH:mm"); end.setAccessibilityLabel("勿扰结束时间，HH:mm")
        quietStartField = start; quietEndField = end
        [startLabel, endLabel, start, end].forEach { form.addSubview($0) }
        let hint = label("按 Mac 当地时间，支持跨午夜", size: 11, color: .secondaryLabelColor)
        hint.frame = NSRect(x: 0, y: 4, width: 340, height: 20); form.addSubview(hint)
        quietFormChanged(); dialog.accessoryView = form
        dialog.addButton(withTitle: "保存"); dialog.addButton(withTitle: "取消")
        dialog.beginSheetModal(for: parent) { response in
            defer { self.quietFormToggle = nil; self.quietStartField = nil; self.quietEndField = nil }
            guard response == .alertFirstButtonReturn,
                  let data = try? JSONSerialization.data(withJSONObject: ["enabled": toggle.state == .on, "start": start.stringValue.trimmingCharacters(in: .whitespaces), "end": end.stringValue.trimmingCharacters(in: .whitespaces)]) else { return }
            self.mutate(["quiet-set"], input: data)
        }
    }

    @objc func diagnoseDevice() {
        guard !busy else { return }
        let id = selectedDeviceID; busy = true; reload()
        DispatchQueue.global(qos: .utility).async {
            let result = backend(["diagnose", "--device-id", id])
            DispatchQueue.main.async {
                self.busy = false; self.reload()
                self.alert("设备诊断结果", reportText(result, devices: self.deviceEntries))
            }
        }
    }

    func memoryDescription() -> String {
        if zeroResident && !batteryEnabled { return "菜单栏与电池监听均已关闭；关闭面板后无界面常驻进程。" }
        var menuMemory = 0
        if let data = try? Data(contentsOf: support.appendingPathComponent("menu-state.json")),
           let d = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            if let pid = d["pid"] as? Int32 {
                var info = proc_taskinfo()
                if proc_pidinfo(pid, PROC_PIDTASKINFO, 0, &info, Int32(MemoryLayout<proc_taskinfo>.size)) > 0 {
                    menuMemory = Int(info.pti_resident_size / 1024 / 1024)
                }
            }
        }
        return "轻量服务约 \(menuMemory) MB · 设置面板约 \(memoryMB()) MB（关闭即释放）\n电池通过系统事件监听；没有定时网络轮询。完全退出 Bark 后停止电池监听。"
    }

    @objc func refresh() {
        reload()
    }
    @objc func closeWindow() { window?.close() }

    func windowWillClose(_ notification: Notification) {
        if let closing = notification.object as? NSWindow, closing === deviceWindow {
            deviceWindow = nil; deviceTable = nil; deviceCountLabel = nil; deviceScroll = nil; deviceScrollHeight = nil
            return
        }
        deviceWindow?.close()
        resourceTimer?.invalidate(); resourceTimer = nil
        window = nil
        stateLabel = nil; detailLabel = nil; lastLabel = nil; memoryLabel = nil
        toggleButton = nil; loginButton = nil; testButton = nil; lowMemoryButton = nil
        pushSwitch = nil; pageControl = nil; pageSlot = nil; normalModeButton = nil; historyText = nil; sourceHint = nil
        sourceTable = nil
        startupSwitch = nil; residentSwitch = nil; batterySwitch = nil; modeHint = nil
        if panelOnly || zeroResident { NSApp.terminate(nil) }
        else { writeMenuStatus() }
    }

    @objc func toggleLowMemory() {
        let newValue = !zeroResident
        preferences.set(newValue, forKey: "zeroResidentMode")
        preferences.synchronize()
        if newValue {
            preferences.set(FileManager.default.fileExists(atPath: loginFile.path), forKey: "loginBeforeZero")
            if !batteryEnabled { try? setLogin(false) }
        } else if preferences.object(forKey: "loginBeforeZero") == nil || preferences.bool(forKey: "loginBeforeZero") {
            try? setLogin(true)
        }
        if !panelOnly { item.isVisible = !newValue }
        DistributedNotificationCenter.default().postNotificationName(.init("CodexBarkModeChanged"), object: nil, userInfo: nil, deliverImmediately: true)
        if (!newValue || batteryEnabled) && panelOnly { startMenu(restore: false) }
        reload()
    }

    func startMenu(restore: Bool) {
        if NSRunningApplication.runningApplications(withBundleIdentifier: "local.codex.bark.menu").contains(where: { !$0.isTerminated }) {
            let name = restore ? "CodexBarkRestoreIcon" : "CodexBarkModeChanged"
            DistributedNotificationCenter.default().postNotificationName(.init(name), object: nil, userInfo: nil, deliverImmediately: true)
            return
        }
        let app = installedAppURL
        let options = NSWorkspace.OpenConfiguration()
        options.activates = false
        options.arguments = ["--background"]
        NSWorkspace.shared.openApplication(at: app, configuration: options) { _, _ in
            if restore {
                DistributedNotificationCenter.default().postNotificationName(.init("CodexBarkRestoreIcon"), object: nil, userInfo: nil, deliverImmediately: true)
            }
        }
    }

    @objc func restoreIcon() {
        if zeroResident {
            preferences.set(false, forKey: "zeroResidentMode")
        }
        preferences.set(400.0, forKey: "NSStatusItem Preferred Position BarkBridgeStatus")
        preferences.set(true, forKey: "NSStatusItem Visible BarkBridgeStatus")
        preferences.synchronize()
        if panelOnly {
            startMenu(restore: true)
            reload()
            sourceHint?.stringValue = "已启用菜单栏模式；Hidden Bar 隐藏时请展开 >。"
            return
        }
        NSStatusBar.system.removeStatusItem(item)
        createStatusItem()
        reload()
        DispatchQueue.main.async { self.writeMenuStatus() }
        sourceHint?.stringValue = "已启用菜单栏模式；Hidden Bar 隐藏时请展开 >。"
    }

    @objc func toggle() {
        if !configured { configurePhone(); return }
        mutate(["set-enabled", "toggle"])
    }

    func setLogin(_ on: Bool) throws {
        let registration = backend(["app-register", "--app", installedAppURL.path], script: "manage.py")
        guard registration["ok"] as? Bool == true else { throw CocoaError(.fileNoSuchFile) }
        let result = backend(["login-set", on ? "on" : "off"], script: "manage.py")
        guard result["ok"] as? Bool == true else { throw CocoaError(.fileWriteUnknown) }
    }

    @objc func toggleLogin() {
        if zeroResident && !batteryEnabled { return }
        do {
            let on = !FileManager.default.fileExists(atPath: loginFile.path)
            try setLogin(on)
            if zeroResident { preferences.set(on, forKey: "loginBeforeZero") }
        }
        catch { alert("无法更新登录启动设置", "请稍后重试。") }
        reload()
    }

    @objc func sendTest() {
        guard configured && enabled && !busy else { return }
        busy = true
        updateWindowIfPresent()
        DispatchQueue.global(qos: .utility).async {
            let result = backend(["test-json"])
            DispatchQueue.main.async {
                self.busy = false
                self.reload()
                self.alert(result["result"] as? String == "accepted" ? "测试已发送" : "测试结果", reportText(result, devices: self.deviceEntries))
            }
        }
    }

    func updateWindowIfPresent() { if window != nil { updateWindow() } }

    @objc func showHistory() {
        navigate(to: 1)
    }

    @objc func configurePhone() {
        showDevices()
        if deviceEntries.isEmpty { addDevice() }
    }

    @objc func showDevices() {
        reload()
        if let existing = deviceWindow {
            existing.makeKeyAndOrderFront(nil); return
        }
        let w = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 400, height: 162),
                         styleMask: [.titled, .closable], backing: .buffered, defer: false)
        w.title = "接收设备"; w.isReleasedWhenClosed = false; w.delegate = self
        deviceWindow = w
        let top = row([label("开启多台，即同时推送", size: 11, color: .secondaryLabelColor), spacer(), button("添加设备…", #selector(addDevice), link: true)])
        let table = NSTableView(); deviceTable = table
        table.rowHeight = 52; table.intercellSpacing = .zero; table.headerView = nil
        table.delegate = self; table.dataSource = self; table.style = .plain
        table.selectionHighlightStyle = .none; table.backgroundColor = .clear; table.focusRingType = .default
        table.setAccessibilityLabel("接收设备列表")
        table.columnAutoresizingStyle = .lastColumnOnlyAutoresizingStyle
        let col = NSTableColumn(identifier: .init("device")); col.width = 360; table.addTableColumn(col)
        let scroll = NSScrollView(); scroll.borderType = .noBorder; scroll.drawsBackground = false
        scroll.hasVerticalScroller = true; scroll.autohidesScrollers = true; scroll.documentView = table
        deviceScroll = scroll
        deviceScrollHeight = scroll.heightAnchor.constraint(equalToConstant: 52)
        deviceScrollHeight?.isActive = true
        deviceCountLabel = label("", size: 11, color: .secondaryLabelColor)
        let bottom = row([deviceCountLabel!, spacer(), button("完成", #selector(closeDevices))])
        let root = column([top, scroll, bottom], spacing: 12)
        root.translatesAutoresizingMaskIntoConstraints = false; w.contentView!.addSubview(root)
        NSLayoutConstraint.activate([
            root.leadingAnchor.constraint(equalTo: w.contentView!.leadingAnchor, constant: 20),
            root.trailingAnchor.constraint(equalTo: w.contentView!.trailingAnchor, constant: -20),
            root.topAnchor.constraint(equalTo: w.contentView!.topAnchor, constant: 18),
            top.widthAnchor.constraint(equalTo: root.widthAnchor),
            scroll.widthAnchor.constraint(equalTo: root.widthAnchor),
            bottom.widthAnchor.constraint(equalTo: root.widthAnchor)
        ])
        w.center(); window?.addChildWindow(w, ordered: .above)
        w.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps: true)
        reload()
    }

    func fitDevices() {
        guard let w = deviceWindow else { return }
        let listHeight = CGFloat(max(1, min(4, deviceEntries.count)) * 52)
        deviceScrollHeight?.constant = listHeight
        deviceScroll?.hasVerticalScroller = deviceEntries.count > 4
        var frame = w.frameRect(forContentRect: NSRect(x: 0, y: 0, width: 400, height: listHeight + 110))
        frame.origin = NSPoint(x: w.frame.minX, y: w.frame.maxY - frame.height)
        if abs(w.frame.height - frame.height) > 1 { w.setFrame(frame, display: true, animate: w.isVisible) }
    }

    func historyCell(_ index: Int) -> NSView? {
        let cell = NSTableCellView(); cell.backgroundStyle = .normal
        let recent = filteredHistory
        guard recent.indices.contains(index) else { return label("没有匹配的记录", size: 12, color: .secondaryLabelColor) }
        let event = recent[index]
        let parts = eventText(event).components(separatedBy: "\n")
        let status = event["status"] as? String ?? ""
        let success = status == "accepted"
        let warning = status == "partial" || status == "send_failed" || status == "not_configured"
        let icon = NSImageView(image: NSImage(systemSymbolName: success ? "checkmark.circle.fill" : (warning ? "exclamationmark.circle" : "circle.dotted"), accessibilityDescription: nil)!)
        icon.contentTintColor = success ? .systemGreen : (warning ? .systemOrange : .secondaryLabelColor)
        icon.translatesAutoresizingMaskIntoConstraints = false
        let titles = column([label(parts.first ?? "通知", size: 12, weight: .medium), historyDetail(nameForSource(event["source_id"] as? String ?? "unknown") + " · " + (parts.count > 1 ? parts[1] : ""))], spacing: 4)
        var cells: [NSView] = [icon, titles, spacer()]
        if let results = event["device_results"] as? [[String: Any]], !results.isEmpty {
            let details = HistoryDetailsButton(title: "详情", target: self, action: #selector(showDeliveryDetails(_:)))
            details.event = event; details.bezelStyle = .inline; details.isBordered = false
            details.font = .systemFont(ofSize: 11); details.contentTintColor = .controlAccentColor
            details.setAccessibilityLabel("查看分设备发送结果")
            details.toolTip = reportText(event, devices: deviceEntries)
            cells.append(details)
        }
        let contents = row(cells, spacing: 10)
        contents.translatesAutoresizingMaskIntoConstraints = false; cell.addSubview(contents)
        NSLayoutConstraint.activate([
            contents.leadingAnchor.constraint(equalTo: cell.leadingAnchor, constant: 4),
            contents.trailingAnchor.constraint(equalTo: cell.trailingAnchor, constant: -4),
            contents.centerYAnchor.constraint(equalTo: cell.centerYAnchor),
            icon.widthAnchor.constraint(equalToConstant: 16), icon.heightAnchor.constraint(equalToConstant: 16)
        ])
        if index < recent.count - 1 {
            let divider = rule(); divider.translatesAutoresizingMaskIntoConstraints = false; cell.addSubview(divider)
            NSLayoutConstraint.activate([
                divider.leadingAnchor.constraint(equalTo: cell.leadingAnchor, constant: 30),
                divider.trailingAnchor.constraint(equalTo: cell.trailingAnchor),
                divider.bottomAnchor.constraint(equalTo: cell.bottomAnchor)
            ])
        }
        return cell
    }

    @objc func showDeliveryDetails(_ sender: HistoryDetailsButton) {
        alert("分设备发送结果", eventText(sender.event) + "\n\n" + reportText(sender.event, devices: deviceEntries))
    }

    @objc func closeDevices() { deviceWindow?.close() }

    func deviceCell(_ index: Int) -> NSView? {
        guard deviceEntries.indices.contains(index) else { return nil }
        let device = deviceEntries[index], id = device["id"] as? String ?? ""
        let name = device["name"] as? String ?? "iPhone"
        let allowed = device["enabled"] as? Bool ?? true
        let cell = NSTableCellView(); cell.backgroundStyle = .normal
        let icon = NSImageView(image: NSImage(systemSymbolName: "iphone", accessibilityDescription: nil)!)
        icon.contentTintColor = .systemTeal; icon.translatesAutoresizingMaskIntoConstraints = false
        let nameLabel = NSTextField(labelWithString: name)
        nameLabel.font = .systemFont(ofSize: 13, weight: .medium); nameLabel.lineBreakMode = .byTruncatingTail
        nameLabel.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        let titles = column([nameLabel, label(allowed ? "参与推送" : "暂停接收", size: 11, color: .secondaryLabelColor)], spacing: 4)
        let toggle = DeviceSwitch(); toggle.deviceID = id; toggle.controlSize = .small
        toggle.target = self; toggle.action = #selector(toggleDevice(_:)); toggle.state = allowed ? .on : .off
        toggle.isEnabled = !busy; toggle.setAccessibilityLabel(name + "接收推送")
        let more = DeviceMenuButton(image: NSImage(systemSymbolName: "ellipsis", accessibilityDescription: nil)!, target: self, action: #selector(deviceMenu(_:)))
        more.deviceID = id; more.isBordered = false; more.bezelStyle = .inline
        more.contentTintColor = .secondaryLabelColor; more.setAccessibilityLabel(name + "设备选项")
        more.widthAnchor.constraint(equalToConstant: 24).isActive = true
        let contents = row([icon, titles, spacer(), toggle, more], spacing: 12)
        contents.translatesAutoresizingMaskIntoConstraints = false; cell.addSubview(contents)
        NSLayoutConstraint.activate([
            contents.leadingAnchor.constraint(equalTo: cell.leadingAnchor, constant: 4),
            contents.trailingAnchor.constraint(equalTo: cell.trailingAnchor, constant: -4),
            contents.centerYAnchor.constraint(equalTo: cell.centerYAnchor),
            icon.widthAnchor.constraint(equalToConstant: 22), icon.heightAnchor.constraint(equalToConstant: 28)
        ])
        return cell
    }

    @objc func toggleDevice(_ sender: DeviceSwitch) {
        mutate(["device-toggle", "--device-id", sender.deviceID, "--device-enabled", sender.state == .on ? "on" : "off"])
    }

    func announceSettings() {
        DistributedNotificationCenter.default().postNotificationName(.init("CodexBarkSettingsChanged"), object: String(getpid()), userInfo: nil, deliverImmediately: true)
    }

    @objc func deviceMenu(_ sender: DeviceMenuButton) {
        selectedDeviceID = sender.deviceID
        guard deviceEntries.contains(where: { $0["id"] as? String == selectedDeviceID }) else { return }
        let menu = NSMenu(); menu.autoenablesItems = false
        for (title, action) in [("测试与诊断…", #selector(diagnoseDevice)), ("重命名…", #selector(renameDevice)), ("移除设备…", #selector(removeDevice))] {
            if title == "移除设备…" { menu.addItem(.separator()) }
            let item = NSMenuItem(title: title, action: action, keyEquivalent: ""); item.target = self
            item.isEnabled = !busy
            menu.addItem(item)
        }
        DispatchQueue.main.async {
            menu.popUp(positioning: nil, at: NSPoint(x: sender.bounds.maxX, y: sender.bounds.maxY), in: sender)
        }
    }

    @objc func addDevice() {
        guard !busy, let parent = deviceWindow else { return }
        let dialog = NSAlert(); dialog.messageText = "添加接收设备"
        dialog.informativeText = "粘贴这台设备 Bark 首页的推送地址。"
        let form = NSView(frame: NSRect(x: 0, y: 0, width: 360, height: 80))
        let name = NSTextField(frame: NSRect(x: 0, y: 48, width: 360, height: 24))
        name.placeholderString = "设备名称，例如：备用 iPhone"; name.setAccessibilityLabel("设备名称")
        let address = NSSecureTextField(frame: NSRect(x: 0, y: 12, width: 360, height: 24))
        address.placeholderString = "Bark HTTPS 推送地址"; address.setAccessibilityLabel("Bark 地址，隐藏输入")
        form.addSubview(name); form.addSubview(address); dialog.accessoryView = form
        dialog.addButton(withTitle: "保存并测试"); dialog.addButton(withTitle: "取消")
        dialog.window.initialFirstResponder = name
        dialog.beginSheetModal(for: parent) { response in
            guard response == .alertFirstButtonReturn else { address.stringValue = ""; return }
            let label = name.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !address.stringValue.isEmpty,
                  let data = try? JSONSerialization.data(withJSONObject: ["name": label.isEmpty ? "iPhone" : label, "address": address.stringValue]) else { return }
            address.stringValue = ""; self.busy = true; self.reload()
            DispatchQueue.global(qos: .utility).async {
                let saved = backend(["device-add"], input: data)
                let result = (saved["id"] as? String).map { backend(["device-test", "--device-id", $0]) } ?? [:]
                DispatchQueue.main.async {
                    self.busy = false; self.reload(); self.announceSettings()
                    if saved["ok"] as? Bool != true { self.alert("未能保存设备", "请使用有效的 Bark HTTPS 推送地址，最多保存 16 台。") }
                    else if result["result"] as? String == "accepted" { self.alert("设备已保存", "测试已被服务器接受，请在这台设备上确认。") }
                    else { self.alert("设备已保存", "测试未成功，请检查推送开关、地址和网络。") }
                }
            }
        }
    }

    @objc func renameDevice() {
        let id = selectedDeviceID
        guard let parent = deviceWindow, let device = deviceEntries.first(where: { $0["id"] as? String == id }) else { return }
        let dialog = NSAlert(); dialog.messageText = "重命名设备"
        let field = NSTextField(frame: NSRect(x: 0, y: 0, width: 300, height: 24))
        field.stringValue = device["name"] as? String ?? "iPhone"; field.setAccessibilityLabel("设备名称")
        dialog.accessoryView = field; dialog.addButton(withTitle: "保存"); dialog.addButton(withTitle: "取消")
        dialog.window.initialFirstResponder = field
        dialog.beginSheetModal(for: parent) { response in
            guard response == .alertFirstButtonReturn,
                  let input = try? JSONSerialization.data(withJSONObject: ["name": field.stringValue]) else { return }
            self.mutate(["device-rename", "--device-id", id], input: input)
        }
    }

    @objc func removeDevice() {
        let id = selectedDeviceID
        guard let parent = deviceWindow else { return }
        let dialog = NSAlert(); dialog.messageText = "移除这个接收设备？"
        dialog.informativeText = "仅删除本机保存的推送地址，重新添加后可继续使用。"
        dialog.addButton(withTitle: "取消"); dialog.addButton(withTitle: "移除")
        dialog.beginSheetModal(for: parent) { response in
            guard response == .alertSecondButtonReturn else { return }
            self.mutate(["device-remove", "--device-id", id])
        }
    }

    @objc func testDevice() {
        let id = selectedDeviceID
        guard !busy else { return }
        busy = true; reload()
        DispatchQueue.global(qos: .utility).async {
            let result = backend(["device-test", "--device-id", id])
            DispatchQueue.main.async {
                self.busy = false; self.reload()
                self.alert(result["ok"] as? Bool == true ? "测试已发送" : "测试未成功",
                           result["ok"] as? Bool == true ? "服务器已接受，请在这台设备上确认。" : "请检查设备开关、地址和网络。")
            }
        }
    }

    func alert(_ title: String, _ body: String) {
        let alert = NSAlert(); alert.messageText = title; alert.informativeText = body
        alert.addButton(withTitle: "好")
        NSApp.activate(ignoringOtherApps: true)
        if let target = deviceWindow?.isVisible == true ? deviceWindow : window { alert.beginSheetModal(for: target) } else { alert.runModal() }
    }

    @objc func uninstall() {
        let dialog = NSAlert()
        dialog.messageText = "卸载本机 Codex Bark？"
        dialog.informativeText = "将移除本机推送入口、手机密钥、菜单栏应用和登录启动项，保留其他 Codex 配置及备份。"
        dialog.addButton(withTitle: "取消")
        dialog.addButton(withTitle: "卸载")
        NSApp.activate(ignoringOtherApps: true)
        guard dialog.runModal() == .alertSecondButtonReturn else { return }
        let p = Process()
        p.executableURL = pythonURL
        p.arguments = [support.appendingPathComponent("manage.py").path, "uninstall"]
        p.standardOutput = FileHandle.nullDevice; p.standardError = FileHandle.nullDevice
        do { try p.run() } catch { alert("未能卸载", "请使用安装说明中的卸载入口。") }
    }

    @objc func quit() { NSApp.terminate(nil) }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
withExtendedLifetime(delegate) { app.run() }
