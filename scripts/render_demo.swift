// Render a silent, captioned 30-second feature simulation using fictional data.
// Usage: swiftc -O scripts/render_demo.swift -o build/render-demo -framework AppKit -framework AVFoundation
//        build/render-demo /absolute/output-directory docs/media/app-icon.png
import AppKit
import AVFoundation
import CoreVideo

let out = URL(fileURLWithPath: CommandLine.arguments[1])
try FileManager.default.createDirectory(at: out, withIntermediateDirectories: true)
let icon = NSImage(contentsOfFile: CommandLine.arguments[2])!
let width = 1280, height = 720, fps = 24, seconds = 30
let mint = NSColor(srgbRed: 0.56, green: 0.88, blue: 0.82, alpha: 1)
let muted = NSColor(srgbRed: 0.67, green: 0.74, blue: 0.83, alpha: 1)

func box(_ rect: NSRect, _ color: NSColor, radius: CGFloat = 16) {
    color.setFill(); NSBezierPath(roundedRect: rect, xRadius: radius, yRadius: radius).fill()
}
func text(_ value: String, _ x: CGFloat, _ y: CGFloat, _ size: CGFloat, _ color: NSColor = .white, weight: NSFont.Weight = .regular, width: CGFloat = 1150) {
    let style = NSMutableParagraphStyle(); style.lineSpacing = 6
    (value as NSString).draw(in: NSRect(x: x, y: y, width: width, height: size * 4.5), withAttributes: [.font: NSFont.systemFont(ofSize: size, weight: weight), .foregroundColor: color, .paragraphStyle: style])
}
func card(_ title: String, _ body: String, x: CGFloat, y: CGFloat, w: CGFloat, accent: NSColor = mint) {
    box(NSRect(x: x, y: y, width: w, height: 130), NSColor(srgbRed: 0.11, green: 0.18, blue: 0.28, alpha: 1))
    box(NSRect(x: x + 20, y: y + 23, width: 4, height: 82), accent, radius: 2)
    text(title, x + 44, y + 22, 21, .white, weight: .semibold, width: w - 65)
    text(body, x + 44, y + 62, 16, muted, width: w - 66)
}
func mac(_ state: Int, progress: Double) {
    let rect = NSRect(x: 80, y: 231, width: 676, height: 328)
    box(rect, NSColor(srgbRed: 0.07, green: 0.12, blue: 0.20, alpha: 1))
    box(NSRect(x: 80, y: 231, width: 676, height: 45), NSColor(srgbRed: 0.15, green: 0.21, blue: 0.30, alpha: 1))
    for (index, color) in [NSColor.systemRed, .systemYellow, .systemGreen].enumerated() {
        box(NSRect(x: CGFloat(98 + index * 19), y: 247, width: 10, height: 10), color, radius: 5)
    }
    text("Codex · 示例项目", 295, 241, 15, muted)
    if state == 0 {
        text("正在处理你的任务…", 112, 306, 27, weight: .medium)
        for i in 0..<4 {
            box(NSRect(x: 114, y: CGFloat(367 + i * 28), width: CGFloat(210 + (i * 71) % 260), height: 8), muted.withAlphaComponent(0.18), radius: 4)
        }
        box(NSRect(x: 113, y: 510, width: 608, height: 5), muted.withAlphaComponent(0.15), radius: 3)
        box(NSRect(x: 113, y: 510, width: 608 * CGFloat(0.15 + progress * 0.68), height: 5), mint, radius: 3)
    } else if state == 1 {
        text("等待你的选择", 112, 305, 26, mint, weight: .semibold)
        text("这次使用哪种方案？", 112, 358, 22)
        box(NSRect(x: 112, y: 412, width: 280, height: 52), .white.withAlphaComponent(0.08), radius: 10)
        box(NSRect(x: 410, y: 412, width: 280, height: 52), .white.withAlphaComponent(0.08), radius: 10)
        text("1. 方案 A", 136, 424, 18); text("2. 方案 B", 434, 424, 18)
        text("实际选择仍在 Mac 上完成", 112, 503, 15, muted)
    } else {
        text("本轮回复已结束", 112, 314, 30, mint, weight: .semibold)
        text("可返回 Mac 查看结果。", 112, 373, 22, muted)
        text("完成提醒可以单独关闭", 112, 496, 16, muted)
    }
    box(NSRect(x: 350, y: 560, width: 135, height: 17), NSColor(srgbRed: 0.18, green: 0.24, blue: 0.31, alpha: 1), radius: 0)
    box(NSRect(x: 285, y: 577, width: 265, height: 7), muted.withAlphaComponent(0.5), radius: 3)
}
func phone(_ state: Int, amount: Double) {
    box(NSRect(x: 864, y: 207, width: 280, height: 398), NSColor(srgbRed: 0.29, green: 0.34, blue: 0.41, alpha: 1), radius: 36)
    box(NSRect(x: 871, y: 214, width: 266, height: 384), NSColor(srgbRed: 0.10, green: 0.17, blue: 0.25, alpha: 1), radius: 30)
    box(NSRect(x: 963, y: 226, width: 80, height: 18), .black, radius: 9)
    text("09:41", 926, 273, 46, weight: .light, width: 210)
    text("iPhone · Bark 提醒示意", 905, 337, 14, muted, width: 225)
    if state > 0 {
        NSGraphicsContext.saveGraphicsState()
        let y = CGFloat(382 + (1 - amount) * 18)
        box(NSRect(x: 885, y: y, width: 238, height: 130), .white.withAlphaComponent(0.12), radius: 18)
        text("BARK", 901, y + 13, 11, mint, weight: .semibold, width: 205)
        text("示例项目 · " + (state == 2 ? "本轮已完成" : "等待回答"), 901, y + 37, 15, .white, weight: .semibold, width: 205)
        text(state == 2 ? "本轮回复已结束。\n返回 Mac 查看结果。" : "这次使用哪种方案？\n1. 方案 A   2. 方案 B", 901, y + 70, 13, muted, width: 205)
        NSGraphicsContext.restoreGraphicsState()
    }
    box(NSRect(x: 963, y: 580, width: 80, height: 4), .white.withAlphaComponent(0.7), radius: 2)
}
let titles = ["任务继续跑，你可以先离开。", "需要你时，知道是哪段聊天。", "一份本机配置，连接你的 iPhone。", "手机看提醒，Mac 上做决定。", "授权、提问、完成，分别控制。", "让工作流提醒，更省心。"]
let subtitles = ["留意授权、问题与本轮完成，不必一直守着屏幕。", "支持的提问事件可带聊天名、短问题与选项。", "已有接入目录换 Codex 账号，手机 Bark 无需改配置。", "推送送达受网络、系统睡眠与手机通知设置影响。", "还支持多设备、每日勿扰、来源静音与低电量提醒。", "Bark Bridge for Mac · MIT 开源 · 2.5.0 开发者预览"]

func drawFrame(_ frame: Int, _ context: CGContext) {
    let time = Double(frame) / Double(fps), scene = min(5, Int(time / 5))
    let local = (time - Double(scene) * 5) / 5
    let gradient = NSGradient(starting: NSColor(srgbRed: 0.07, green: 0.15, blue: 0.22, alpha: 1), ending: NSColor(srgbRed: 0.04, green: 0.07, blue: 0.13, alpha: 1))!
    gradient.draw(in: NSRect(x: 0, y: 0, width: width, height: height), angle: 25)
    text("BARK BRIDGE  /  MAC → IPHONE", 64, 36, 13, mint, weight: .semibold)
    text("功能示意 · 模拟演示", 1014, 36, 13, muted, width: 245)
    text(titles[scene], 64, 92, 38, weight: .semibold)
    text(subtitles[scene], 66, 153, 18, muted)
    if scene < 5 {
        mac(scene == 0 ? 0 : (scene == 4 ? 2 : 1), progress: local)
        phone(scene >= 3 ? (scene == 4 ? 2 : 1) : 0, amount: min(1, local * 3))
        if scene >= 2 {
            for i in 0..<4 {
                let offset = (local * 90 + Double(i) * 18).truncatingRemainder(dividingBy: 90)
                box(NSRect(x: 770 + offset, y: 395, width: 5, height: 5), mint.withAlphaComponent(0.25 + offset / 120), radius: 3)
            }
        }
    } else {
        icon.draw(in: NSRect(x: 82, y: 245, width: 136, height: 136))
        text("Bark Bridge for Mac", 257, 256, 38, weight: .semibold)
        text("下载预览 → 打开安装助手 → 添加手机 → 测试", 260, 322, 23, mint)
        card("无需源码编译", "Apple Silicon · 内置 Python · 首次安装专用", x: 82, y: 425, w: 535)
        card("按自己的节奏接收", "多设备、勿扰与提醒开关，随时可调整", x: 639, y: 425, w: 535)
        text("github.com/dneo9527-ctrl/bark-bridge-macos", 84, 585, 20, muted)
    }
    text("示意不代表手机实收 · 普通文字提问可能遗漏 · App 未经 Apple 公证", 66, 660, 14, muted)
    for i in 0..<6 { box(NSRect(x: CGFloat(1090 + i * 21), y: 665, width: 12, height: 4), i == scene ? mint : muted.withAlphaComponent(0.25), radius: 2) }
    box(NSRect(x: 0, y: 716, width: Double(width) * time / Double(seconds), height: 4), mint, radius: 0)
}

let movieURL = out.appendingPathComponent("Bark-Bridge-30s-demo.mp4")
let writer = try AVAssetWriter(outputURL: movieURL, fileType: .mp4)
let input = AVAssetWriterInput(mediaType: .video, outputSettings: [AVVideoCodecKey: AVVideoCodecType.h264, AVVideoWidthKey: width, AVVideoHeightKey: height, AVVideoCompressionPropertiesKey: [AVVideoAverageBitRateKey: 2400000, AVVideoMaxKeyFrameIntervalKey: fps * 2]])
input.expectsMediaDataInRealTime = false
let attributes: [String: Any] = [kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32ARGB, kCVPixelBufferWidthKey as String: width, kCVPixelBufferHeightKey as String: height, kCVPixelBufferCGImageCompatibilityKey as String: true, kCVPixelBufferCGBitmapContextCompatibilityKey as String: true]
let adaptor = AVAssetWriterInputPixelBufferAdaptor(assetWriterInput: input, sourcePixelBufferAttributes: attributes)
writer.add(input); writer.startWriting(); writer.startSession(atSourceTime: .zero)
for frame in 0..<(fps * seconds) {
    autoreleasepool {
        while !input.isReadyForMoreMediaData { Thread.sleep(forTimeInterval: 0.002) }
        var optional: CVPixelBuffer?
        CVPixelBufferPoolCreatePixelBuffer(nil, adaptor.pixelBufferPool!, &optional)
        let pixel = optional!; CVPixelBufferLockBaseAddress(pixel, [])
        let context = CGContext(data: CVPixelBufferGetBaseAddress(pixel), width: width, height: height, bitsPerComponent: 8, bytesPerRow: CVPixelBufferGetBytesPerRow(pixel), space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.noneSkipFirst.rawValue)!
        context.translateBy(x: 0, y: CGFloat(height)); context.scaleBy(x: 1, y: -1)
        NSGraphicsContext.saveGraphicsState(); NSGraphicsContext.current = NSGraphicsContext(cgContext: context, flipped: true)
        drawFrame(frame, context)
        if frame % (fps * 5) == fps * 2, let image = context.makeImage() {
            let bitmap = NSBitmapImageRep(cgImage: image)
            try! bitmap.representation(using: .png, properties: [:])!.write(to: out.appendingPathComponent("scene-\(frame / (fps * 5) + 1).png"))
        }
        NSGraphicsContext.restoreGraphicsState()
        CVPixelBufferUnlockBaseAddress(pixel, [])
        if !adaptor.append(pixel, withPresentationTime: CMTime(value: Int64(frame), timescale: Int32(fps))) { fatalError("Frame encoding failed") }
    }
}
input.markAsFinished(); writer.endSession(atSourceTime: CMTime(value: Int64(seconds), timescale: 1))
let semaphore = DispatchSemaphore(value: 0)
writer.finishWriting { semaphore.signal() }; semaphore.wait()
guard writer.status == .completed else { fatalError("Video encoding failed") }
print("Rendered 30s H.264 silent feature simulation: " + movieURL.path)
